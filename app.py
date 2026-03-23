#!/usr/bin/env python3
"""app.py — Auto Sim web interface"""

import copy
import json
import threading
from pathlib import Path

import requests as req_lib
from flask import Flask, jsonify, request, render_template

from droptimizer import (
    RAIDBOTS_BASE,
    RAIDBOTS_HEADERS,
    apply_talent,
    build_payload,
    find_talent_builds,
    fetch_character,
    fetch_encounter_items,
    get_site_versions,
    poll_job,
    submit_job,
)

app = Flask(__name__)

CHARS_PATH  = Path(__file__).parent / "characters.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
REPORT_URL  = RAIDBOTS_BASE + "/simbot/report/{sim_id}"

# ---------------------------------------------------------------------------
# Global run state
# ---------------------------------------------------------------------------

_state: dict = {"running": False, "jobs": [], "log": []}
_lock = threading.Lock()


def _log(msg: str) -> None:
    with _lock:
        _state["log"].append(msg)


def _update_job(job_id: str, **kw) -> None:
    with _lock:
        for j in _state["jobs"]:
            if j["id"] == job_id:
                j.update(kw)
                break


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

def _run(jobs: list, chars_by_id: dict, raidsid: str) -> None:
    try:
        session = req_lib.Session()
        session.headers.update(RAIDBOTS_HEADERS)
        if raidsid:
            session.cookies.set("raidsid", raidsid, domain="www.raidbots.com")

        _log("Fetching static data...")
        static_hash, frontend_version = get_site_versions(session)

        _log("Fetching encounter items...")
        encounter_items = fetch_encounter_items(session, static_hash)

        resp      = session.get(f"{RAIDBOTS_BASE}/static/data/{static_hash}/instances.json", timeout=15)
        instances = resp.json()
        _log(f"Ready — {len(jobs)} job(s) queued.")

        for job in jobs:
            char = chars_by_id[job["char_id"]]
            jid  = job["id"]
            tag  = job["label"]

            _update_job(jid, status="fetching")
            _log(f"[{tag}] Fetching character from armory...")
            try:
                character = fetch_character(session, char["region"], char["realm"], char["name"])
            except Exception as e:
                _log(f"[{tag}] Character fetch failed: {e}")
                _update_job(jid, status="failed")
                continue

            # Use talent-build-specific simc if the job specifies one
            simc = char["simc_string"]
            if job.get("talent_code"):
                simc = apply_talent(simc, job["talent_code"])

            cfg_wrap = {
                "character":   {"name": char["name"], "realm": char["realm"], "region": char["region"]},
                "simc_string": simc,
            }
            run_opts = {
                "difficulty":    job["difficulty"],
                "instance_id":   -91,
                "spec":          char["spec"],
                "spec_id":       char.get("spec_id", 63),
                "loot_spec_id":  char.get("loot_spec_id", char.get("spec_id", 63)),
                "fight_style":   "Patchwerk",
                "iterations":    "smart",
                "crafted_stats": char.get("crafted_stats", "36/49"),
            }

            _update_job(jid, status="submitting")
            _log(f"[{tag}] Submitting...")
            try:
                payload        = build_payload(cfg_wrap, run_opts, character, encounter_items, instances, frontend_version)
                sim_id, _      = submit_job(session, payload, None)
            except Exception as e:
                _log(f"[{tag}] Submit failed: {e}")
                _update_job(jid, status="failed")
                continue

            _update_job(jid, status="running", sim_id=sim_id)
            _log(f"[{tag}] Running ({sim_id})...")

            ok  = poll_job(session, sim_id, timeout_minutes=30)
            url = REPORT_URL.format(sim_id=sim_id)
            if ok:
                _log(f"[{tag}] Done.")
                _update_job(jid, status="done", url=url)
            else:
                _log(f"[{tag}] Failed or timed out.")
                _update_job(jid, status="failed", url=url)

    except Exception as e:
        _log(f"Unexpected error: {e}")
    finally:
        with _lock:
            _state["running"] = False
        _log("— finished —")


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
    save_characters(chars)
    return jsonify(char)


@app.delete("/api/characters/<char_id>")
def api_delete_character(char_id):
    save_characters([c for c in load_characters() if c["id"] != char_id])
    return jsonify({"ok": True})


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
    with _lock:
        if _state["running"]:
            return jsonify({"error": "Already running"}), 409

    selections  = request.json.get("selections", [])
    chars_all   = load_characters()
    chars_by_id = {c["id"]: c for c in chars_all}

    jobs = []
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
                jobs.append({
                    "id":           job_id,
                    "char_id":      sel["char_id"],
                    "label":        f"{char['name']} \u2013 {char['spec']}{build_suffix} \u2013 {diff_label}",
                    "difficulty":   diff,
                    "talent_code":  talent_code,
                    "status":       "pending",
                    "sim_id":       None,
                    "url":          None,
                })

    if not jobs:
        return jsonify({"error": "Nothing selected"}), 400

    with _lock:
        _state["running"] = True
        _state["jobs"]    = jobs
        _state["log"]     = []

    threading.Thread(target=_run, args=(jobs, chars_by_id, load_raidsid()), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with _lock:
        return jsonify({
            "running": _state["running"],
            "jobs":    copy.deepcopy(_state["jobs"]),
            "log":     list(_state["log"]),
        })


if __name__ == "__main__":
    import webbrowser, threading as _t
    _t.Timer(0.8, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(debug=False, port=5000, use_reloader=False)
