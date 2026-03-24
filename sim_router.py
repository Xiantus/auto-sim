"""sim_router.py — Route simulations to the correct backend.

Determines whether a spec should use Raidbots Droptimizer (DPS/tank) or
QuestionablyEpic Upgrade Finder (healers), and provides thin wrappers that
execute a single simulation job on the chosen backend.

Playwright (used by QE) is imported lazily inside ``run_qe_sim`` so machines
that only run DPS/tank sims never need Playwright installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    import requests
    from payload_builder import CharacterIdentity, SimTarget, StaticData

log = logging.getLogger(__name__)

# Human-readable label for each difficulty string understood by the system.
DIFF_LABELS: dict[str, str] = {
    "raid-normal":              "Normal",
    "raid-heroic":              "Heroic",
    "raid-mythic":              "Mythic",
    "dungeon-mythic7":          "M+7",
    "dungeon-mythic10":         "M+10",
    "dungeon-mythic-weekly10":  "M+10 Vault",
}


def diff_label(difficulty: str) -> str:
    """Return a short human-readable label for a difficulty string."""
    return DIFF_LABELS.get(difficulty, difficulty)


# Spec IDs that should be routed to QE instead of Raidbots.
# Kept here as the authoritative source; qe_sim.py re-exports for its own use.
_HEALER_SPEC_IDS: frozenset[int] = frozenset({
    65,   # Holy Paladin
    105,  # Restoration Druid
    256,  # Discipline Priest
    257,  # Holy Priest
    264,  # Restoration Shaman
    270,  # Mistweaver Monk
    1468, # Preservation Evoker
})


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    """Result of a single simulation run."""
    label: str
    url:   str
    ok:    bool
    error: str = ""


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def backend_for(spec_id: int) -> str:
    """Return ``"qe"`` for healer specs, ``"raidbots"`` for all others."""
    return "qe" if int(spec_id) in _HEALER_SPEC_IDS else "raidbots"


def is_healer(spec_id: int) -> bool:
    """Return ``True`` if *spec_id* is a healer spec."""
    return backend_for(spec_id) == "qe"


# ---------------------------------------------------------------------------
# Backend wrappers
# ---------------------------------------------------------------------------

def run_qe_sim(simc: str, label: str = "Heroic + Mythic", spec_id: int = 0, timeout_minutes: int = 5) -> SimResult:
    """Run a QuestionablyEpic Upgrade Finder simulation.

    Playwright is imported lazily here so callers that only run DPS sims are
    not forced to have it installed.

    Args:
        simc:            Full SimC string for the character.
        label:           Human-readable label for the result (default shown in
                         the Discord/web UI).
        timeout_minutes: How long to wait for QE to finish.

    Returns:
        A :class:`SimResult` with ``ok=True`` and the report URL on success.
    """
    try:
        from qe_sim import run_qe_upgradefinder  # lazy import
        url = run_qe_upgradefinder(simc, spec_id=spec_id, timeout_minutes=timeout_minutes)
        return SimResult(label=label, url=url, ok=True)
    except Exception as exc:
        log.error("QE sim failed: %s", exc)
        return SimResult(label=label, url="", ok=False, error=str(exc))


def run_raidbots_sim(
    session:   "requests.Session",
    identity:  "CharacterIdentity",
    target:    "SimTarget",
    character: dict,
    static:    "StaticData",
    report_url_template: str = "https://www.raidbots.com/simbot/report/{sim_id}",
    timeout_minutes: int = 30,
    on_submitted: Optional[Callable[[str], None]] = None,
) -> SimResult:
    """Submit a Raidbots Droptimizer job and poll until it completes.

    Args:
        session:              An authenticated :class:`requests.Session`.
        identity:             Character name/realm/region/spec/simc.
        target:               Simulation parameters (difficulty, spec IDs…).
        character:            Raw ``/wowapi/character`` response from Raidbots.
        static:               Pre-fetched encounter items, instances, version.
        report_url_template:  URL template with ``{sim_id}`` placeholder.
        timeout_minutes:      How long to poll before giving up.

    Returns:
        A :class:`SimResult` with the Raidbots report URL.
    """
    from droptimizer import submit_job, poll_job   # lazy — avoids circular at module level
    from payload_builder import build_payload

    label = diff_label(target.difficulty)

    try:
        payload   = build_payload(identity, target, character, static)
        sim_id, _ = submit_job(session, payload, None)
    except Exception as exc:
        log.exception("Raidbots submit failed")
        return SimResult(label=label, url="", ok=False, error=str(exc))

    if on_submitted is not None:
        try:
            on_submitted(sim_id)
        except Exception as exc:
            log.warning("on_submitted callback raised: %s", exc)

    ok  = poll_job(session, sim_id, timeout_minutes=timeout_minutes)
    url = report_url_template.format(sim_id=sim_id)
    return SimResult(label=label, url=url, ok=ok)
