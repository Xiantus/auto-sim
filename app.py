#!/usr/bin/env python3
"""app.py — Auto Sim web interface"""

import json
import logging
import secrets
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests as _requests
from flask import Flask, jsonify, request, render_template, session, Response

log = logging.getLogger(__name__)

from droptimizer import (
    RAIDBOTS_BASE,
    apply_talent,
    fetch_character,
    fetch_static_data,
    find_talent_builds,
)
from payload_builder import CharacterIdentity, DIFFICULTY_MAP, SimTarget
from raidbots_session import make_raidbots_session
from sim_router import diff_label as get_diff_label, is_healer, run_qe_sim, run_raidbots_sim
from job_state import Job, JobStatus, SimRunnerState
import db
from auth import auth_bp, require_login

app = Flask(__name__)
app.register_blueprint(auth_bp)

CONFIG_PATH  = Path(__file__).parent / "config.json"
RESULTS_PATH = Path(__file__).parent / "results.json"
REPORT_URL   = RAIDBOTS_BASE + "/simbot/report/{sim_id}"

# ---------------------------------------------------------------------------
# Secret key — generated once and persisted in config.json
# ---------------------------------------------------------------------------

def _get_or_create_secret_key() -> str:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    if "secret_key" not in cfg:
        cfg["secret_key"] = secrets.token_hex(32)
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass
    return cfg["secret_key"]


app.secret_key = _get_or_create_secret_key()
app.permanent_session_lifetime = timedelta(days=30)

db.init_db()

# ---------------------------------------------------------------------------
# Global run state
# ---------------------------------------------------------------------------

state = SimRunnerState(RESULTS_PATH)


def _log(msg: str) -> None:
    state.append_log(msg)


def _jlog(job_id: str, msg: str) -> None:
    """Log to both the per-job log and the global log."""
    state.append_job_log(job_id, msg)
    state.append_log(msg)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

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


def load_wow_savedvars_path() -> str:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text()).get("wow_savedvars_path", "")
        except Exception:
            pass
    return ""


def save_wow_savedvars_path(path: str) -> None:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    cfg["wow_savedvars_path"] = path
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Tooltip / SavedVariables helpers
# ---------------------------------------------------------------------------

def _parse_tooltip_data(report_json: dict) -> list[dict]:
    """Extract item upgrade entries from a Raidbots Droptimizer report JSON.

    Tries multiple field-name spellings across Raidbots API versions.
    Returns a list of {item_id, dps_gain, ilvl, item_name} dicts.
    """
    try:
        players = report_json.get("sim", {}).get("players", [])
        if not players:
            return []
        droptimizer = players[0].get("droptimizer", {})
        upgrades    = droptimizer.get("upgrades", [])
        entries = []
        for upg in upgrades:
            item_node = upg.get("item", {})
            item_id  = (upg.get("itemId") or upg.get("itemID")
                        or item_node.get("id"))
            dps_gain = (upg.get("dpsGain") or upg.get("dps")
                        or upg.get("upgrade") or upg.get("value"))
            ilvl     = upg.get("ilvl") or upg.get("itemLevel")
            name     = (upg.get("name") or upg.get("itemName")
                        or item_node.get("name", ""))
            if item_id and dps_gain is not None and float(dps_gain) > 0:
                entries.append({
                    "item_id":   int(item_id),
                    "dps_gain":  round(float(dps_gain), 1),
                    "ilvl":      int(ilvl) if ilvl else None,
                    "item_name": str(name) if name else None,
                })
        return entries
    except Exception as exc:
        log.warning("tooltip parse error: %s", exc)
        return []


def _build_lua(user_id: int) -> str:
    """Generate the Lua SavedVariables content for a user's tooltip data."""
    data     = db.load_tooltip_data_for_user(user_id)
    now      = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines    = [
        f"-- Simdragosa SavedVariables",
        f"-- Generated: {now}",
        f"-- Do not edit manually — regenerated after each sim run.",
        f"",
        f"SimdragosaDB = {{",
    ]
    for char_key, items in sorted(data.items()):
        lines.append(f'  ["{char_key}"] = {{')
        for item_id, info in sorted(items.items()):
            parts = []
            if "heroic" in info:
                parts.append(f"heroic={info['heroic']}")
            if "mythic" in info:
                parts.append(f"mythic={info['mythic']}")
            if info.get("ilvl"):
                parts.append(f"ilvl={info['ilvl']}")
            if info.get("name"):
                safe_name = info["name"].replace('"', '\\"')
                parts.append(f'name="{safe_name}"')
            parts.append(f'updated="{info.get("updated", "")}"')
            lines.append(f"    [{item_id}] = {{ {', '.join(parts)} }},")
        lines.append("  },")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write_savedvariables(user_id: int) -> None:
    """Write Simdragosa.lua to the configured WoW SavedVariables folder."""
    wow_path = load_wow_savedvars_path()
    if not wow_path:
        return
    target = Path(wow_path) / "Simdragosa.lua"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_build_lua(user_id), encoding="utf-8")
        log.info("SavedVariables written → %s", target)
    except Exception as exc:
        log.warning("Could not write SavedVariables: %s", exc)


def _fetch_and_store_tooltip_data(
    session_rb,
    char: dict,
    difficulty: str,
    sim_id: str,
    user_id: int,
) -> None:
    """Fetch the Raidbots report JSON, parse DPS gains, persist and write Lua."""
    try:
        url  = f"{RAIDBOTS_BASE}/simbot/report/{sim_id}/data.json"
        resp = session_rb.get(url, timeout=30)
        if not resp.ok:
            log.warning("Could not fetch report data for %s: HTTP %s", sim_id, resp.status_code)
            return
        entries = _parse_tooltip_data(resp.json())
        if not entries:
            log.info("No droptimizer upgrades found in report %s", sim_id)
            return
        sim_date = datetime.utcnow().strftime("%Y-%m-%d")
        db.upsert_tooltip_entries(
            user_id=user_id,
            char_name=char["name"],
            realm=char.get("realm", ""),
            difficulty=difficulty,
            entries=entries,
            sim_date=sim_date,
        )
        log.info("Stored %d tooltip entries for %s (%s)", len(entries), char["name"], difficulty)
        _write_savedvariables(user_id)
    except Exception as exc:
        log.warning("tooltip data fetch/store failed: %s", exc)


# ---------------------------------------------------------------------------
# Background sim runner
# ---------------------------------------------------------------------------

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


def _run_one(job: Job, char: dict, raidsid: str, static, user_id: int | None = None) -> None:
    jid     = job.id
    tag     = job.label
    spec_id = char.get("spec_id", 63)

    if is_healer(spec_id):
        if job.difficulty != "raid-heroic":
            state.transition(jid, JobStatus.SKIPPED)
            return

        simc = char["simc_string"]
        if job.talent_code:
            simc = apply_talent(simc, job.talent_code)

        state.transition(jid, JobStatus.RUNNING)
        _jlog(jid, f"[{tag}] Running QE Upgrade Finder (Heroic + Mythic)...")
        result = run_qe_sim(simc, spec_id=spec_id)
        if result.ok:
            _jlog(jid, f"[{tag}] Done.")
            state.transition(jid, JobStatus.DONE, url=result.url,
                             label=tag.replace("– Heroic", "– Heroic + Mythic"))
        else:
            _jlog(jid, f"[{tag}] QE failed: {result.error}")
            state.transition(jid, JobStatus.FAILED)
        return

    session_rb = make_raidbots_session(raidsid)

    state.transition(jid, JobStatus.FETCHING)
    _jlog(jid, f"[{tag}] Fetching character from armory...")
    try:
        character = fetch_character(session_rb, char["region"], char["realm"], char["name"])
    except Exception as e:
        _jlog(jid, f"[{tag}] Character fetch failed: {e}")
        state.transition(jid, JobStatus.FAILED)
        return

    simc = char["simc_string"]
    if job.talent_code:
        simc = apply_talent(simc, job.talent_code)

    spec_name = char["spec"].capitalize()
    if spec_name not in _VALID_SPEC_NAMES:
        spec_name = _SPEC_ID_TO_NAME.get(spec_id, "Fire")
        _jlog(jid, f"[{tag}] Spec '{char['spec']}' unrecognised — using '{spec_name}' from spec_id {spec_id}.")

    diff_cfg = DIFFICULTY_MAP.get(job.difficulty, DIFFICULTY_MAP["raid-heroic"])
    identity = CharacterIdentity(
        name=char["name"], realm=char["realm"], region=char["region"],
        spec_label=spec_name, simc_string=simc,
    )
    target = SimTarget(
        difficulty=job.difficulty,
        instance_id=diff_cfg["instance_id"],
        fight_style=diff_cfg["fight_style"],
        spec_id=spec_id,
        loot_spec_id=char.get("loot_spec_id", spec_id),
        crafted_stats=char.get("crafted_stats", "36/49"),
    )

    def _on_submitted(sim_id: str) -> None:
        state.transition(jid, JobStatus.RUNNING, sim_id=sim_id)
        _jlog(jid, f"[{tag}] Running ({sim_id})...")

    state.transition(jid, JobStatus.SUBMITTING)
    _jlog(jid, f"[{tag}] Submitting...")
    result = run_raidbots_sim(
        session_rb, identity, target, character, static,
        report_url_template=REPORT_URL,
        timeout_minutes=30,
        on_submitted=_on_submitted,
    )
    if result.ok:
        _jlog(jid, f"[{tag}] Done.")
        state.transition(jid, JobStatus.DONE, url=result.url)
        if user_id is not None:
            sim_id = result.url.rstrip("/").split("/")[-1]
            threading.Thread(
                target=_fetch_and_store_tooltip_data,
                args=(session_rb, char, job.difficulty, sim_id, user_id),
                daemon=True,
            ).start()
    else:
        if result.error:
            _jlog(jid, f"[{tag}] {result.error}")
        _jlog(jid, f"[{tag}] Failed or timed out.")
        state.transition(jid, JobStatus.FAILED, url=result.url or "")


def _run_batch(jobs: list[Job], chars_by_id: dict, raidsid: str, user_id: int | None = None) -> None:
    try:
        init_session = make_raidbots_session(raidsid)

        _log("Fetching static data...")
        static = fetch_static_data(init_session)
        _log(f"Starting {len(jobs)} job(s)...")

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                pool.submit(_run_one, job, chars_by_id[job.char_id], raidsid, static, user_id): job
                for job in jobs
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    job = futures[future]
                    _log(f"[{job.label}] Unexpected error: {e}")

    except Exception as e:
        _log(f"Unexpected error in batch: {e}")


# ---------------------------------------------------------------------------
# Gear propagation helpers
# ---------------------------------------------------------------------------

_GEAR_SLOTS = ["head", "neck", "shoulder", "back", "chest", "wrist", "hands",
               "waist", "legs", "feet", "finger1", "finger2",
               "trinket1", "trinket2", "mainHand", "offHand"]

def _calc_ilvl(items: dict) -> float | None:
    if not items:
        return None
    total, count = 0, 0
    main = items.get("mainHand")
    for slot in _GEAR_SLOTS:
        item = items.get(slot)
        if slot == "offHand" and item is None and isinstance(main, dict) and main.get("inventoryType") == 17:
            item = main
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
        if updated_ilvl and existing_ilvl and updated_ilvl == existing_ilvl:
            continue
        old_gear = _simc_gear_lines(c.get("simc_string", ""))
        if old_gear == new_gear:
            continue
        c["simc_string"] = _replace_simc_gear(c["simc_string"], new_gear)
        c["ilvl"] = updated_ilvl
        changed.append(c["id"])
    return changed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
@require_login
def index():
    return render_template("index.html")


@app.get("/api/characters")
@require_login
def api_get_characters():
    return jsonify(db.load_characters(session["user_id"]))


@app.post("/api/characters")
@require_login
def api_upsert_character():
    user_id = session["user_id"]
    char = request.json
    char["id"] = f"{char['name'].lower()}-{char['spec'].lower()}"
    db.upsert_character(user_id, char)
    chars = db.load_characters(user_id)
    propagated = _propagate_gear(char, chars)
    for c in chars:
        if c["id"] in propagated:
            db.upsert_character(user_id, c)
    return jsonify({"char": char, "propagated": propagated})


@app.delete("/api/characters/<char_id>")
@require_login
def api_delete_character(char_id):
    db.delete_character(session["user_id"], char_id)
    return jsonify({"ok": True})


@app.get("/api/ilvl/<char_id>")
@require_login
def api_get_ilvl(char_id):
    user_id = session["user_id"]
    chars = db.load_characters(user_id)
    char  = next((c for c in chars if c["id"] == char_id), None)
    if not char:
        return jsonify({"error": "not found"}), 404
    if char.get("ilvl"):
        return jsonify({"ilvl": char["ilvl"]})
    try:
        rb_session = make_raidbots_session(load_raidsid())
        data = fetch_character(rb_session, char["region"], char["realm"], char["name"])
        ilvl = _calc_ilvl(data.get("items", {}))
        if ilvl:
            db.update_character_ilvl(user_id, char_id, ilvl)
        return jsonify({"ilvl": ilvl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/settings")
@require_login
def api_get_settings():
    sid      = load_raidsid()
    masked   = sid[:12] + "…" if len(sid) > 12 else sid
    wow_path = load_wow_savedvars_path()
    return jsonify({
        "raidsid_masked":    masked,
        "has_raidsid":       bool(sid),
        "wow_savedvars_path": wow_path,
    })


@app.post("/api/settings")
@require_login
def api_save_settings():
    data = request.json
    if "raidsid" in data:
        save_raidsid(data["raidsid"])
    if "wow_savedvars_path" in data:
        save_wow_savedvars_path(data["wow_savedvars_path"])
    return jsonify({"ok": True})


@app.get("/api/tooltip-export")
@require_login
def api_tooltip_export():
    """Return the Simdragosa.lua content as a downloadable file."""
    user_id = session["user_id"]
    lua     = _build_lua(user_id)
    return Response(
        lua,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=Simdragosa.lua"},
    )


@app.get("/api/tooltip-debug")
@require_login
def api_tooltip_debug():
    """Fetch the data.json for the most recent completed sim and return its
    raw structure so we can verify field names and fix the parser."""
    # Find the most recent DONE job with a Raidbots URL
    snap    = state.snapshot()
    results = snap.get("results", [])   # results is a list, not a dict
    url     = None
    for entry in results:
        job = entry.get("last_success") or entry.get("latest") or {}
        if job.get("url") and "raidbots.com" in job.get("url", ""):
            url = job["url"]
            break

    if not url:
        return jsonify({"error": "No completed Raidbots sim found. Run a sim first."}), 404

    sim_id    = url.rstrip("/").split("/")[-1]
    data_url  = f"{RAIDBOTS_BASE}/simbot/report/{sim_id}/data.json"

    try:
        rb_session = make_raidbots_session(load_raidsid())
        resp       = rb_session.get(data_url, timeout=30)
        if not resp.ok:
            return jsonify({"error": f"Raidbots returned HTTP {resp.status_code}", "url": data_url}), 502
        raw = resp.json()
    except Exception as exc:
        return jsonify({"error": str(exc), "url": data_url}), 500

    # Extract the pieces the parser cares about, without sending the whole 1MB blob
    players    = raw.get("sim", {}).get("players", [])
    player_0   = players[0] if players else {}
    droptimizer = player_0.get("droptimizer", {})
    upgrades   = droptimizer.get("upgrades", [])

    return jsonify({
        "report_url":        url,
        "data_url":          data_url,
        "player_name":       player_0.get("name"),
        "player_keys":       list(player_0.keys()),
        "droptimizer_keys":  list(droptimizer.keys()),
        "upgrade_count":     len(upgrades),
        # First 3 raw upgrade objects so we can see every field name
        "upgrades_sample":   upgrades[:3],
    })


@app.post("/api/run")
@require_login
def api_run():
    user_id     = session["user_id"]
    selections  = request.json.get("selections", [])
    chars_all   = db.load_characters(user_id)
    chars_by_id = {c["id"]: c for c in chars_all}

    jobs: list[Job] = []
    for sel in selections:
        char = chars_by_id.get(sel["char_id"])
        if not char:
            continue

        talent_builds = find_talent_builds(char.get("simc_string", ""))
        if not talent_builds:
            talent_builds = {"": None}

        for build_label, talent_code in talent_builds.items():
            for diff in sel.get("difficulties", []):
                build_suffix = f" \u2013 {build_label}" if build_label else ""
                job_id       = f"{sel['char_id']}-{build_label.lower() or 'default'}-{diff}-{int(time.time())}"
                jobs.append(Job(
                    id=job_id,
                    char_id=sel["char_id"],
                    label=f"{char['name']} \u2013 {char['spec']}{build_suffix} \u2013 {get_diff_label(diff)}",
                    difficulty=diff,
                    build_label=build_label,
                    talent_code=talent_code,
                ))

    if not jobs:
        return jsonify({"error": "Nothing selected"}), 400

    state.add_jobs(jobs)
    threading.Thread(
        target=_run_batch,
        args=(jobs, chars_by_id, load_raidsid(), session["user_id"]),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.get("/api/status")
@require_login
def api_status():
    return jsonify(state.snapshot())


if __name__ == "__main__":
    import webbrowser, threading as _t

    try:
        import discord_bot as _db_mod
        _cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        _bot_token = _cfg.get("discord_bot_token")
        if _bot_token:
            _t.Thread(target=_db_mod.start, args=(_bot_token,), daemon=True).start()
    except Exception as _e:
        print(f"[discord] Bot not started: {_e}")

    _t.Timer(0.8, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", debug=False, port=5000, use_reloader=False)
