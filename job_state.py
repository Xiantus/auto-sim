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
- ``persist_path`` injected at construction — pass ``Path("/dev/null")`` in tests.
"""

from __future__ import annotations

import copy
import json
import logging
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
    talent_code:  Optional[str] = None
    status:       JobStatus     = JobStatus.PENDING
    sim_id:       Optional[str] = None
    url:          Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "id":          self.id,
            "char_id":     self.char_id,
            "label":       self.label,
            "difficulty":  self.difficulty,
            "talent_code": self.talent_code,
            "status":      self.status.value,
            "sim_id":      self.sim_id,
            "url":         self.url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            id=d["id"],
            char_id=d["char_id"],
            label=d["label"],
            difficulty=d["difficulty"],
            talent_code=d.get("talent_code"),
            status=JobStatus(d.get("status", "pending")),
            sim_id=d.get("sim_id"),
            url=d.get("url"),
        )


# Observer type: called with (job_after_transition, old_status)
JobObserver = Callable[[Job, JobStatus], None]


# ---------------------------------------------------------------------------
# SimRunnerState
# ---------------------------------------------------------------------------

class SimRunnerState:
    """Thread-safe run state container with enforced transition DAG.

    Args:
        persist_path: Path to write/read ``last_run.json``.  Pass
                      ``Path("/dev/null")`` or ``Path("nul")`` in tests to
                      suppress all file I/O.
    """

    def __init__(self, persist_path: Path) -> None:
        self._persist_path = persist_path
        self._lock         = threading.RLock()
        self._running      = False
        self._jobs:  list[Job] = []
        self._log:   list[str] = []
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

    def start_run(self, jobs: list[Job]) -> None:
        """Begin a new run.  Raises :exc:`RuntimeError` if already running."""
        with self._lock:
            if self._running:
                raise RuntimeError("A run is already in progress.")
            self._running = True
            self._jobs = list(jobs)
            self._log  = []

    def finish_run(self) -> None:
        """Mark the run as finished and persist state to disk."""
        with self._lock:
            self._running = False
            snapshot = self._snapshot_unsafe()
        self._persist(snapshot)

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
            job_copy = copy.copy(job)

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
        """Append *msg* to the run log."""
        with self._lock:
            self._log.append(msg)

    # ------------------------------------------------------------------
    # Read-only access
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict:
        """Return a deep copy of current state safe for JSON serialisation."""
        with self._lock:
            return self._snapshot_unsafe()

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

    def _snapshot_unsafe(self) -> dict:
        """Caller must hold _lock."""
        return {
            "running": self._running,
            "jobs":    [j.as_dict() for j in self._jobs],
            "log":     list(self._log),
        }

    def _persist(self, snapshot: dict) -> None:
        try:
            self._persist_path.write_text(json.dumps(snapshot, indent=2))
        except Exception as exc:
            log.warning("Could not persist run state to %s: %s", self._persist_path, exc)

    def _load(self) -> None:
        """Load last-run state from disk on startup (best-effort)."""
        try:
            data = json.loads(self._persist_path.read_text())
            self._jobs = [Job.from_dict(d) for d in data.get("jobs", [])]
            self._log  = data.get("log", [])
        except Exception:
            pass
