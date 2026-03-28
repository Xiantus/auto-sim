"""job_state.py — Typed job state machine with enforced transition DAG.

All mutable run state lives here.  ``app.py`` creates a single module-level
:class:`SimRunnerState` instance; route handlers and the background worker
interact with it through the public API rather than mutating a raw dict.

Design goals
------------
- One :class:`threading.RLock` owned by ``SimRunnerState`` — never exposed.
- Transition DAG enforced: invalid transitions raise :exc:`InvalidTransitionError`.
- Observers notified after each transition (used for e.g. Discord push).
- ``snapshot()`` returns a deep copy so callers cannot mutate live state.
- ``persist_path`` injected at construction — pass ``Path("/dev/null")`` in tests to
  suppress all file I/O.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status enum and transition graph
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING    = "pending"
    FETCHING   = "fetching"
    SUBMITTING = "submitting"
    RUNNING    = "running"
    DONE       = "done"
    FAILED     = "failed"
    CANCELLED  = "cancelled"
    SKIPPED    = "skipped"   # healer mythic twin — silently collapsed into heroic job


# Allowed transitions.  Terminal statuses (DONE/FAILED/CANCELLED/SKIPPED) have
# no outgoing edges and will raise InvalidTransitionError if transitioned from.
_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING:    frozenset({
        JobStatus.FETCHING,
        JobStatus.SUBMITTING,   # healer path skips FETCHING
        JobStatus.RUNNING,      # healer path skips directly to RUNNING
        JobStatus.SKIPPED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }),
    JobStatus.FETCHING:   frozenset({
        JobStatus.SUBMITTING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }),
    JobStatus.SUBMITTING: frozenset({
        JobStatus.RUNNING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }),
    JobStatus.RUNNING:    frozenset({
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }),
}

_TERMINAL: frozenset[JobStatus] = frozenset({
    JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED,
})


class InvalidTransitionError(ValueError):
    """Raised when a requested status transition is not in the allowed DAG."""


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """A single simulation job (one character × one difficulty × one talent build)."""
    id:           str
    char_id:      str
    label:        str
    difficulty:   str
    build_label:  str            = ""
    talent_code:  Optional[str] = None
    status:       JobStatus     = JobStatus.PENDING
    sim_id:       Optional[str] = None
    url:          Optional[str] = None
    started_at:   Optional[float] = None
    completed_at: Optional[float] = None
    log_lines:    list           = field(default_factory=list)
    user_id:      Optional[int]  = None

    def as_dict(self) -> dict:
        return {
            "id":           self.id,
            "char_id":      self.char_id,
            "label":        self.label,
            "difficulty":   self.difficulty,
            "build_label":  self.build_label,
            "talent_code":  self.talent_code,
            "status":       self.status.value,
            "sim_id":       self.sim_id,
            "url":          self.url,
            "started_at":   self.started_at,
            "completed_at": self.completed_at,
            "log_lines":    list(self.log_lines),
            "user_id":      self.user_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            id=d["id"],
            char_id=d["char_id"],
            label=d["label"],
            difficulty=d["difficulty"],
            build_label=d.get("build_label", ""),
            talent_code=d.get("talent_code"),
            status=JobStatus(d.get("status", "done")),
            sim_id=d.get("sim_id"),
            url=d.get("url"),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            log_lines=d.get("log_lines", []),
            user_id=d.get("user_id"),
        )


# Observer type: called with (job_after_transition, old_status)
JobObserver = Callable[[Job, JobStatus], None]


# ---------------------------------------------------------------------------
# SimRunnerState
# ---------------------------------------------------------------------------

class SimRunnerState:
    """Thread-safe run state container with enforced transition DAG.

    Args:
        persist_path: Path to write/read ``results.json``.  Pass
                      ``Path("/dev/null")`` or ``Path("nul")`` in tests to
                      suppress all file I/O.
    """

    def __init__(self, persist_path: Path) -> None:
        self._persist_path = persist_path
        self._lock         = threading.RLock()
        self._jobs:    list[Job]        = []
        self._results: dict[str, dict]  = {}   # key → {latest: Job, last_success: Job|None}
        self._log:     list[str]        = []
        self._observers: list[JobObserver] = []
        self._load()

    # ------------------------------------------------------------------
    # Observer API
    # ------------------------------------------------------------------

    def add_observer(self, observer: JobObserver) -> None:
        """Register a callback invoked after every job transition."""
        with self._lock:
            self._observers.append(observer)

    def _notify(self, job: Job, old_status: JobStatus) -> None:
        for obs in self._observers:
            try:
                obs(job, old_status)
            except Exception as exc:
                log.warning("Observer %r raised: %s", obs, exc)

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def add_jobs(self, jobs: list[Job]) -> None:
        """Append new jobs to the active pool.  Safe to call while running."""
        with self._lock:
            self._jobs.extend(jobs)

    # ------------------------------------------------------------------
    # State mutations
    # ------------------------------------------------------------------

    def transition(
        self,
        job_id:     str,
        new_status: JobStatus,
        *,
        sim_id: Optional[str] = None,
        url:    Optional[str] = None,
        label:  Optional[str] = None,
    ) -> Job:
        """Transition *job_id* to *new_status*.

        Raises :exc:`InvalidTransitionError` if the transition is not allowed.
        Raises :exc:`KeyError` if *job_id* is not found.
        Fires all registered observers after the transition.
        """
        with self._lock:
            job = self._get_job(job_id)
            old_status = job.status
            allowed = _TRANSITIONS.get(old_status, frozenset())
            if new_status not in allowed:
                raise InvalidTransitionError(
                    f"Cannot transition job {job_id!r} from {old_status!r} to {new_status!r}. "
                    f"Allowed: {sorted(s.value for s in allowed)}"
                )
            job.status = new_status
            if sim_id is not None:
                job.sim_id = sim_id
            if url is not None:
                job.url = url
            if label is not None:
                job.label = label

            # Set started_at on first transition out of PENDING
            if old_status == JobStatus.PENDING and job.started_at is None:
                job.started_at = time.time()

            # Set completed_at and update results store on terminal transition
            if new_status in _TERMINAL:
                job.completed_at = time.time()
                self._update_results(job)

            job_copy = copy.deepcopy(job)

        self._notify(job_copy, old_status)
        return job_copy

    def cancel(self, job_id: str) -> Job:
        """Cancel a single non-terminal job."""
        with self._lock:
            job = self._get_job(job_id)
            if job.status in _TRANSITIONS:   # non-terminal
                return self.transition(job_id, JobStatus.CANCELLED)
            return copy.copy(job)

    def cancel_all(self) -> list[str]:
        """Cancel all non-terminal jobs.  Returns list of cancelled job IDs."""
        cancelled = []
        with self._lock:
            ids = [j.id for j in self._jobs if j.status in _TRANSITIONS]
        for jid in ids:
            try:
                self.cancel(jid)
                cancelled.append(jid)
            except Exception:
                pass
        return cancelled

    def append_log(self, msg: str) -> None:
        """Append *msg* to the global run log."""
        with self._lock:
            self._log.append(msg)

    def append_job_log(self, job_id: str, msg: str) -> None:
        """Append *msg* to the per-job log for *job_id*."""
        with self._lock:
            try:
                self._get_job(job_id).log_lines.append(msg)
            except KeyError:
                pass

    # ------------------------------------------------------------------
    # Read-only access
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return any(j.status not in _TERMINAL for j in self._jobs)

    def snapshot(self) -> dict:
        """Return a deep copy of current state safe for JSON serialisation."""
        with self._lock:
            return self._snapshot_unsafe()

    def snapshot_for_user(self, user_id: int) -> dict:
        """Return a snapshot filtered to only the requesting user's jobs."""
        with self._lock:
            snap = self._snapshot_unsafe()
        snap["active_jobs"] = [j for j in snap["active_jobs"] if j.get("user_id") == user_id]
        snap["results"] = [
            r for r in snap["results"]
            if r.get("latest", {}).get("user_id") == user_id
        ]
        snap["log"] = []
        return snap

    def get_job(self, job_id: str) -> Job:
        """Return a copy of the job with *job_id*.  Raises :exc:`KeyError` if not found."""
        with self._lock:
            return copy.copy(self._get_job(job_id))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_job(self, job_id: str) -> Job:
        """Caller must hold _lock."""
        for j in self._jobs:
            if j.id == job_id:
                return j
        raise KeyError(f"Job {job_id!r} not found.")

    def _results_key(self, job: Job) -> str:
        return f"{job.char_id}|{job.difficulty}|{job.build_label}"

    def _update_results(self, job: Job) -> None:
        """Caller must hold _lock.  Update persisted results store."""
        key = self._results_key(job)
        existing = self._results.get(key, {"last_success": None})
        self._results[key] = {
            "latest":       copy.deepcopy(job),
            "last_success": copy.deepcopy(job) if job.status == JobStatus.DONE
                            else existing.get("last_success"),
        }
        self._persist_results()

    def _snapshot_unsafe(self) -> dict:
        """Caller must hold _lock."""
        active = [j.as_dict() for j in self._jobs if j.status not in _TERMINAL]
        results = [
            {
                "key":          k,
                "latest":       v["latest"].as_dict(),
                "last_success": v["last_success"].as_dict() if v["last_success"] else None,
            }
            for k, v in self._results.items()
        ]
        return {
            "running":     any(j.status not in _TERMINAL for j in self._jobs),
            "active_jobs": active,
            "results":     results,
            "log":         list(self._log),
        }

    def _persist_results(self) -> None:
        """Caller must hold _lock."""
        try:
            data = {
                k: {
                    "latest":       v["latest"].as_dict(),
                    "last_success": v["last_success"].as_dict() if v["last_success"] else None,
                }
                for k, v in self._results.items()
            }
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            log.warning("Could not persist results to %s: %s", self._persist_path, exc)

    def _load(self) -> None:
        """Load persisted results from disk on startup (best-effort)."""
        try:
            data = json.loads(self._persist_path.read_text())
            for k, v in data.items():
                latest = Job.from_dict(v["latest"]) if v.get("latest") else None
                last_success = Job.from_dict(v["last_success"]) if v.get("last_success") else None
                if latest:
                    self._results[k] = {"latest": latest, "last_success": last_success}
        except Exception:
            pass
