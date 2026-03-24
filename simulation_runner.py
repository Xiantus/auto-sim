"""simulation_runner.py — Unified orchestration layer for droptimizer sims.

:class:`SimulationRunner` is the single entry point used by the Discord bot
and the CLI runner.  It owns:

- SimC string parsing (name, region, realm, spec, class)
- Character preset resolution from ``characters.json``
- Healer/DPS routing (delegates to :mod:`sim_router`)
- Parallel job fan-out
- Async wrapper for Discord's event loop

Usage
-----
::

    from simulation_runner import SimulationRunner, RunnerConfig
    from sim_router import SimResult

    config  = RunnerConfig(raidsid="abc…")
    runner  = SimulationRunner(config)
    results = runner.run(simc)          # blocking
    results = await runner.run_async(simc)  # from async code
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Authoritative class/spec → spec_id mapping used when characters.json has no preset.
SPEC_IDS: dict[str, dict[str, int]] = {
    "death_knight": {"blood": 250, "frost": 251, "unholy": 252},
    "demon_hunter": {"havoc": 577, "vengeance": 581, "devourer": 1480},
    "druid":        {"balance": 102, "feral": 103, "guardian": 104, "restoration": 105},
    "evoker":       {"devastation": 1467, "preservation": 1468, "augmentation": 1473},
    "hunter":       {"beast_mastery": 253, "marksmanship": 254, "survival": 255},
    "mage":         {"arcane": 62, "fire": 63, "frost": 64},
    "monk":         {"brewmaster": 268, "mistweaver": 270, "windwalker": 269},
    "paladin":      {"holy": 65, "protection": 66, "retribution": 70},
    "priest":       {"discipline": 256, "holy": 257, "shadow": 258},
    "rogue":        {"assassination": 259, "outlaw": 260, "subtlety": 261},
    "shaman":       {"elemental": 262, "enhancement": 263, "restoration": 264},
    "warlock":      {"affliction": 265, "demonology": 266, "destruction": 267},
    "warrior":      {"arms": 71, "fury": 72, "protection": 73},
}

_DEFAULT_CHARS_PATH  = Path(__file__).parent / "characters.json"
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunnerConfig:
    """Immutable configuration for a :class:`SimulationRunner` instance."""
    raidsid:         str           = ""
    difficulties:    tuple[str, ...] = ("raid-heroic", "raid-mythic")
    timeout_minutes: int           = 30


# ---------------------------------------------------------------------------
# SimC parsing helpers
# ---------------------------------------------------------------------------

def parse_simc(simc: str) -> dict:
    """Extract ``name``, ``char_class``, ``region``, ``realm``, ``spec`` from SimC."""
    result: dict = {}
    for line in simc.splitlines():
        m = re.match(r'^([\w][\w\s]*)="([^"]+)"', line)
        if m and "char_class" not in result:
            result["char_class"] = m.group(1).strip().lower().replace(" ", "_")
            result["name"]       = m.group(2).strip()
        kv = re.match(r'^(\w+)\s*=\s*(.+)', line)
        if not kv:
            continue
        k, v = kv.group(1), kv.group(2).strip()
        if k == "region": result["region"] = v.lower()
        if k == "server": result["realm"]  = v.lower()
        if k == "spec":   result["spec"]   = v.lower()
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class SimulationRunner:
    """Run all talent-build × difficulty combinations for a SimC string.

    Args:
        config:      Immutable run parameters.
        chars_path:  Path to ``characters.json`` for preset resolution.
                     Defaults to the file next to this module.
    """

    def __init__(
        self,
        config:     RunnerConfig = RunnerConfig(),
        chars_path: Path         = _DEFAULT_CHARS_PATH,
    ) -> None:
        self._config     = config
        self._chars_path = chars_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, simc: str) -> list:
        """Run all sims for *simc* and return a list of :class:`~sim_router.SimResult`.

        Raises :exc:`ValueError` if the SimC string cannot be parsed.
        """
        from sim_router import SimResult  # local to avoid circular import at module level
        info = parse_simc(simc)
        if not all(k in info for k in ("name", "region", "realm", "spec")):
            raise ValueError("Could not parse name / region / realm / spec from SimC string.")

        preset, spec_id, loot_spec_id, crafted_stats, simc_final = \
            self._resolve_preset(info, simc)

        from sim_router import is_healer
        if is_healer(spec_id):
            return self._run_healer(simc_final, spec_id=spec_id)
        return self._run_dps(info, simc_final, spec_id, loot_spec_id, crafted_stats)

    async def run_async(self, simc: str) -> list:
        """Async wrapper — runs :meth:`run` in a thread-pool executor.

        Suitable for use inside a Discord.py async event handler without
        blocking the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, simc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_characters(self) -> list:
        try:
            return json.loads(self._chars_path.read_text())
        except Exception:
            return []

    def _resolve_preset(self, info: dict, simc: str):
        """Look up a saved preset matching name + spec."""
        saved = {
            (c["name"].lower(), c["spec"].lower()): c
            for c in self._load_characters()
        }
        preset        = saved.get((info["name"].lower(), info["spec"].lower()))
        spec_id       = (preset.get("spec_id")      if preset else None) or \
                        SPEC_IDS.get(info.get("char_class", ""), {}).get(info["spec"], 63)
        loot_spec_id  = (preset.get("loot_spec_id") if preset else None) or spec_id
        crafted_stats = (preset.get("crafted_stats") if preset else None) or "36/49"
        simc_final    = (preset.get("simc_string")   if preset else None) or simc
        return preset, spec_id, loot_spec_id, crafted_stats, simc_final

    def _run_healer(self, simc_final: str, spec_id: int = 0) -> list:
        from droptimizer import apply_talent, find_talent_builds
        from sim_router import run_qe_sim

        talent_builds = find_talent_builds(simc_final) or {"": None}

        def _qe_one(build_label: str, talent_code) -> dict:
            sim_simc = apply_talent(simc_final, talent_code) if talent_code else simc_final
            label    = f"Heroic + Mythic{' – ' + build_label if build_label else ''}"
            r = run_qe_sim(sim_simc, label=label, spec_id=spec_id,
                           timeout_minutes=self._config.timeout_minutes)
            return {"label": r.label, "url": r.url, "ok": r.ok, "error": r.error}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(talent_builds)) as pool:
            futures = [pool.submit(_qe_one, bl, tc) for bl, tc in talent_builds.items()]
            return [f.result() for f in concurrent.futures.as_completed(futures)]

    def _run_dps(
        self, info: dict, simc_final: str,
        spec_id: int, loot_spec_id: int, crafted_stats: str,
    ) -> list:
        from droptimizer import apply_talent, fetch_character, fetch_static_data, find_talent_builds
        from payload_builder import CharacterIdentity, SimTarget
        from raidbots_session import make_raidbots_session
        from sim_router import run_raidbots_sim
        from droptimizer import RAIDBOTS_BASE

        report_url_tmpl = RAIDBOTS_BASE + "/simbot/report/{sim_id}"
        init_session = make_raidbots_session(self._config.raidsid)
        static       = fetch_static_data(init_session)
        character    = fetch_character(init_session, info["region"], info["realm"], info["name"])

        talent_builds = find_talent_builds(simc_final) or {"": None}
        jobs = [
            (build_label, talent_code, difficulty)
            for build_label, talent_code in talent_builds.items()
            for difficulty in self._config.difficulties
        ]

        def _one(build_label: str, talent_code, difficulty: str) -> dict:
            from payload_builder import DIFFICULTY_MAP
            from sim_router import diff_label as get_diff_label
            s        = make_raidbots_session(self._config.raidsid)
            sim_simc = apply_talent(simc_final, talent_code) if talent_code else simc_final
            label    = f"{get_diff_label(difficulty)}{' \u2013 ' + build_label if build_label else ''}"
            diff_cfg = DIFFICULTY_MAP.get(difficulty, DIFFICULTY_MAP["raid-heroic"])
            identity = CharacterIdentity(
                name=info["name"], realm=info["realm"], region=info["region"],
                spec_label=info["spec"].capitalize(), simc_string=sim_simc,
            )
            target = SimTarget(
                difficulty=difficulty,
                instance_id=diff_cfg["instance_id"],
                fight_style=diff_cfg["fight_style"],
                spec_id=spec_id,
                loot_spec_id=loot_spec_id,
                crafted_stats=crafted_stats,
            )
            r = run_raidbots_sim(s, identity, target, character, static,
                                 report_url_template=report_url_tmpl,
                                 timeout_minutes=self._config.timeout_minutes)
            return {"label": label, "url": r.url, "ok": r.ok}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(_one, *j) for j in jobs]
            return [f.result() for f in concurrent.futures.as_completed(futures)]
