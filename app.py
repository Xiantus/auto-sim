#!/usr/bin/env python3
"""app.py — Auto Sim web interface"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, jsonify, request, render_template

from droptimizer import (
    RAIDBOTS_BASE,
    apply_talent,
    fetch_character,
    fetch_static_data,
    find_talent_builds,
    poll_job,
    submit_job,
)
from payload_builder import CharacterIdentity, SimTarget, build_payload
from raidbots_session import make_raidbots_session
from sim_router import is_healer, run_qe_sim, run_raidbots_sim
from job_state import Job, JobStatus, SimRunnerState

app = Flask(__name__)

CHARS_PATH    = Path(__file__).parent / "characters.json"
CONFIG_PATH   = Path(__file__).parent / "config.json"
LAST_RUN_PATH = Path(__file__).parent / "last_run.json"
REPORT_URL    = RAIDBOTS_BASE + "/simbot/report/{sim_id}"

# ---------------------------------------------------------------------------
# Global run state
# ---------------------------------------------------------------------------

state = SimRunnerState(LAST_RUN_PATH)


def _log(msg: str) -> None:
    state.append_log(msg)



# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_characters() -> list:
    if CHARS_PATH.exists():
        return json.loads(CHARS_PATH.read_text())
    # Seed from existing config.json on first run
    if CONFIG_PATH.exists():
        try:
            cfg  = json.loads(CONFIG_PATH.read_text())
            char = cfg.get("character", {})
            simc = cfg.get("simc_string", "")
            runs = cfg.get("runs", [])
            if char and simc and runs:
                first = runs[0]
                return [{
                    "id":            f"{char['name'].lower()}-{first.get('spec','').lower()}",
                    "name":          char["name"],
                    "realm":         char["realm"],
                    "region":        char["region"],
                    "spec":          first.get("spec", ""),
                    "spec_id":       first.get("spec_id", 63),
                    "loot_spec_id":  first.get("loot_spec_id", first.get("spec_id", 63)),
                    "crafted_stats": first.get("crafted_stats", "36/49"),
                    "simc_string":   simc,
                }]
        except Exception:
            pass
    return []


def save_characters(chars: list) -> None:
    CHARS_PATH.write_text(json.dumps(chars, indent=2))


def load_raidsid() -> str:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text()).get("raidsid", "")
        except Exception:
            pass
    return ""


def save_raidsid(raidsid: str) -> None:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    cfg["raidsid"] = raidsid
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Background sim runner
# ---------------------------------------------------------------------------

# spec_id → Raidbots spec name (used as fallback when char["spec"] is wrong)
_SPEC_ID_TO_NAME: dict[int, str] = {
    62: "Arcane", 63: "Fire", 64: "Frost",
    65: "Holy", 66: "Protection", 70: "Retribution",
    71: "Arms", 72: "Fury", 73: "Protection",
    102: "Balance", 103: "Feral", 104: "Guardian", 105: "Restoration",
    250: "Blood", 251: "Frost", 252: "Unholy",
    253: "BeastMastery", 254: "Marksmanship", 255: "Survival",
    256: "Discipline", 257: "Holy", 258: "Shadow",
    259: "Assassination", 260: "Outlaw", 261: "Subtlety",
    262: "Elemental", 263: "Enhancement", 264: "Restoration",
    265: "Affliction", 266: "Demonology", 267: "Destruction",
    268: "Brewmaster", 269: "Windwalker", 270: "Mistweaver",
    577: "Havoc", 581: "Vengeance", 1480: "Devourer",
    1467: "Devastation", 1468: "Preservation", 1473: "Augmentation",
}

_VALID_SPEC_NAMES: set[str] = set(_SPEC_ID_TO_NAME.values())



def _run_one(job: Job, char: dict, raidsid: str, static) -> None:
    jid     = job.id
    tag     = job.label
    spec_id = char.get("spec_id", 63)

    # ── Healer: use QE Upgrade Finder (one combined Heroic+Mythic report) ────
    if is_healer(spec_id):
        # QE runs both difficulties in one session; only process the heroic job
        # and skip the mythic duplicate to avoid running the browser twice.
        if job.difficulty != "raid-heroic":
            state.transition(jid, JobStatus.SKIPPED)
            return

        simc = char["simc_string"]
        if job.talent_code:
            simc = apply_talent(simc, job.talent_code)

        state.transition(jid, JobStatus.RUNNING)
        _log(f"[{tag}] Running QE Upgrade Finder (Heroic + Mythic)...")
        result = run_qe_sim(simc)
        if result.ok:
            _log(f"[{tag}] Done.")
            state.transition(jid, JobStatus.DONE, url=result.url,
                             label=tag.replace("– Heroic", "– Heroic + Mythic"))
        else:
            _log(f"[{tag}] QE failed: {result.error}")
            state.transition(jid, JobStatus.FAILED)
        return

    # ── DPS / Tank: use Raidbots Droptimizer ────────────────────────────────
    session = make_raidbots_session(raidsid)

    state.transition(jid, JobStatus.FETCHING)
    _log(f"[{tag}] Fetching character from armory...")
    try:
        character = fetch_character(session, char["region"], char["realm"], char["name"])
    except Exception as e:
        _log(f"[{tag}] Character fetch failed: {e}")
        state.transition(jid, JobStatus.FAILED)
        return

    simc = char["simc_string"]
    if job.talent_code:
        simc = apply_talent(simc, job.talent_code)

    # Derive the canonical spec name from spec_id if char["spec"] is not a
    # recognised Raidbots spec name (e.g. it was accidentally set to the
    # character name).
    spec_name = char["spec"].capitalize()
    if spec_name not in _VALID_SPEC_NAMES:
        spec_name = _SPEC_ID_TO_NAME.get(spec_id, "Fire")
        _log(f"[{tag}] Spec '{char['spec']}' unrecognised — using '{spec_name}' from spec_id {spec_id}.")

    identity = CharacterIdentity(
        name=char["name"], realm=char["realm"], region=char["region"],
        spec_label=spec_name, simc_string=simc,
    )
    target = SimTarget(
        difficulty=job.difficulty,
        spec_id=spec_id,
        loot_spec_id=char.get("loot_spec_id", spec_id),
        crafted_stats=char.get("crafted_stats", "36/49"),
    )

    state.transition(jid, JobStatus.SUBMITTING)
    _log(f"[{tag}] Submitting...")
    try:
        payload   = build_payload(identity, target, character, static)
        sim_id, _ = submit_job(session, payload, None)
    except Exception as e:
        _log(f"[{tag}] Submit failed: {e}")
        state.transition(jid, JobStatus.FAILED)
        return

    state.transition(jid, JobStatus.RUNNING, sim_id=sim_id)
    _log(f"[{tag}] Running ({sim_id})...")

    ok  = poll_job(session, sim_id, timeout_minutes=30)
    url = REPORT_URL.format(sim_id=sim_id)
    if ok:
        _log(f"[{tag}] Done.")
        state.transition(jid, JobStatus.DONE, url=url)
    else:
        _log(f"[{tag}] Failed or timed out.")
        state.transition(jid, JobStatus.FAILED, url=url)


def _run(jobs: list[Job], chars_by_id: dict, raidsid: str) -> None:
    try:
        init_session = make_raidbots_session(raidsid)

        _log("Fetching static data...")
        static = fetch_static_data(init_session)
        _log(f"Ready — {len(jobs)} job(s) queued, running in parallel.")

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                pool.submit(_run_one, job, chars_by_id[job.char_id], raidsid, static): job
                for job in jobs
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    job = futures[future]
                    _log(f"[{job.label}] Unexpected error: {e}")

    except Exception as e:
        _log(f"Unexpected error: {e}")
    finally:
        _log("— finished —")
        state.finish_run()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GEAR_SLOTS = ["head", "neck", "shoulder", "back", "chest", "wrist", "hands",
               "waist", "legs", "feet", "finger1", "finger2",
               "trinket1", "trinket2", "mainHand", "offHand"]

def _calc_ilvl(items: dict) -> float | None:
    """Calculate average equipped ilvl with 2 dp, counting 2H weapons twice."""
    if not items:
        return None
    total, count = 0, 0
    main = items.get("mainHand")
    for slot in _GEAR_SLOTS:
        item = items.get(slot)
        if slot == "offHand" and item is None and isinstance(main, dict) and main.get("inventoryType") == 17:
            item = main  # 2H weapon fills the off-hand slot too
        if isinstance(item, dict) and item.get("itemLevel"):
            total += item["itemLevel"]
            count += 1
    if count != 16:
        return None
    return round(total / 16, 2)


_SIMC_GEAR_SLOTS = {
    "head", "neck", "shoulder", "back", "chest", "wrist", "hands",
    "waist", "legs", "feet", "finger1", "finger2",
    "trinket1", "trinket2", "main_hand", "off_hand",
}

def _simc_gear_lines(simc: str) -> list[str]:
    return [l for l in simc.splitlines()
            if l.split("=")[0].strip() in _SIMC_GEAR_SLOTS]

def _replace_simc_gear(simc: str, new_gear: list[str]) -> str:
    non_gear = [l for l in simc.splitlines()
                if l.split("=")[0].strip() not in _SIMC_GEAR_SLOTS]
    body = "\n".join(non_gear).rstrip()
    return body + "\n\n" + "\n".join(new_gear)

def _propagate_gear(updated: dict, chars: list) -> list[str]:
    """Push gear from `updated` to same-name profiles. Returns list of updated ids."""
    new_gear = _simc_gear_lines(updated.get("simc_string", ""))
    if not new_gear:
        return []
    updated_name = updated.get("name", "").lower()
    updated_ilvl  = updated.get("ilvl")
    changed = []
    for c in chars:
        if c["id"] == updated["id"]:
            continue
        if c.get("name", "").lower() != updated_name:
            continue
        if c.get("exclude_from_item_updates"):
            continue
        existing_ilvl = c.get("ilvl")
        # Only propagate if ilvl differs (or either side has no ilvl cached)
        if updated_ilvl and existing_ilvl and updated_ilvl == existing_ilvl:
            continue
        old_gear = _simc_gear_lines(c.get("simc_string", ""))
        if old_gear == new_gear:
            continue
        c["simc_string"] = _replace_simc_gear(c["simc_string"], new_gear)
        c["ilvl"] = updated_ilvl  # carry over cached ilvl
        changed.append(c["id"])
    return changed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/characters")
def api_get_characters():
    return jsonify(load_characters())


@app.post("/api/characters")
def api_upsert_character():
    char = request.json
    char["id"] = f"{char['name'].lower()}-{char['spec'].lower()}"
    chars = load_characters()
    idx = next((i for i, c in enumerate(chars) if c["id"] == char["id"]), None)
    if idx is not None:
        chars[idx] = char
    else:
        chars.append(char)
    propagated = _propagate_gear(char, chars)
    save_characters(chars)
    return jsonify({"char": char, "propagated": propagated})


@app.delete("/api/characters/<char_id>")
def api_delete_character(char_id):
    save_characters([c for c in load_characters() if c["id"] != char_id])
    return jsonify({"ok": True})


@app.get("/api/ilvl/<char_id>")
def api_get_ilvl(char_id):
    chars = load_characters()
    char  = next((c for c in chars if c["id"] == char_id), None)
    if not char:
        return jsonify({"error": "not found"}), 404
    # Return cached value if already stored
    if char.get("ilvl"):
        return jsonify({"ilvl": char["ilvl"]})
    # Fetch from armory
    try:
        session = make_raidbots_session(load_raidsid())
        data = fetch_character(session, char["region"], char["realm"], char["name"])
        ilvl = _calc_ilvl(data.get("items", {}))
        if ilvl:
            # Cache in character profile
            for c in chars:
                if c["id"] == char_id:
                    c["ilvl"] = ilvl
            save_characters(chars)
        return jsonify({"ilvl": ilvl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/settings")
def api_get_settings():
    sid = load_raidsid()
    masked = sid[:12] + "…" if len(sid) > 12 else sid
    return jsonify({"raidsid_masked": masked, "has_raidsid": bool(sid)})


@app.post("/api/settings")
def api_save_settings():
    data = request.json
    if "raidsid" in data:
        save_raidsid(data["raidsid"])
    return jsonify({"ok": True})


@app.post("/api/run")
def api_run():
    selections  = request.json.get("selections", [])
    chars_all   = load_characters()
    chars_by_id = {c["id"]: c for c in chars_all}

    jobs: list[Job] = []
    for sel in selections:
        char = chars_by_id.get(sel["char_id"])
        if not char:
            continue

        # Detect named talent builds (Raid / ST) in the SimC string
        talent_builds = find_talent_builds(char.get("simc_string", ""))
        # If no named builds found, run with the single active talent (None = no override)
        if not talent_builds:
            talent_builds = {"": None}

        for build_label, talent_code in talent_builds.items():
            for diff in sel.get("difficulties", []):
                diff_label   = "Heroic" if diff == "raid-heroic" else "Mythic"
                build_suffix = f" \u2013 {build_label}" if build_label else ""
                job_id       = f"{sel['char_id']}-{build_label.lower() or 'default'}-{diff}"
                jobs.append(Job(
                    id=job_id,
                    char_id=sel["char_id"],
                    label=f"{char['name']} \u2013 {char['spec']}{build_suffix} \u2013 {diff_label}",
                    difficulty=diff,
                    talent_code=talent_code,
                ))

    if not jobs:
        return jsonify({"error": "Nothing selected"}), 400

    try:
        state.start_run(jobs)
    except RuntimeError:
        return jsonify({"error": "Already running"}), 409

    threading.Thread(target=_run, args=(jobs, chars_by_id, load_raidsid()), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    return jsonify(state.snapshot())


if __name__ == "__main__":
    import webbrowser, threading as _t

    # Start Discord bot if token is configured
    try:
        import discord_bot as _db
        _cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        _bot_token = _cfg.get("discord_bot_token")
        if _bot_token:
            _t.Thread(target=_db.start, args=(_bot_token,), daemon=True).start()
    except Exception as _e:
        print(f"[discord] Bot not started: {_e}")

    _t.Timer(0.8, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000, use_reloader=False)
