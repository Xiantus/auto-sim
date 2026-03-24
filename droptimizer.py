#!/usr/bin/env python3
"""
droptimizer.py
--------------
Submits one or more Raidbots Droptimizer jobs for your WoW character,
polls until each completes, then DMs each report link to a target Discord bot
using the Discord bot API ("/wishlist <url>").

Run manually or via cron / Task Scheduler.
"""

import time
import json
import logging
import re
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Build names we recognise as "raid" or "single-target"
_RAID_NAMES = {'raid', 'raid build', 'raid st', 'raidst'}
_ST_NAMES   = {'st', 'single target', 'single_target', 'singletarget', 'patchwerk'}


def find_talent_builds(simc: str) -> dict[str, str]:
    """
    Parse a SimC string for named talent builds.

    Supports two formats:

    1. copy= blocks (standard SimC multi-profile export):
         copy="Xiage_Raid",Xiage
         talents=CODE

    2. Section-header comments followed by a (possibly commented-out) talent line:
         # --- Raid ---
         # talents=CODE
         ### Single Target
         talents=CODE

    Returns a dict mapping canonical label → talent_code, e.g.:
        {"Raid": "C8DA...", "ST": "CAEAMh..."}
    Only builds whose name matches _RAID_NAMES or _ST_NAMES are returned.
    """
    result: dict[str, str] = {}

    def _canonical(name: str) -> str | None:
        n = name.strip().lower().replace('-', ' ').replace('_', ' ')
        if n in _RAID_NAMES:
            return 'Raid'
        if n in _ST_NAMES:
            return 'ST'
        return None

    lines = simc.splitlines()

    # --- Format 1: copy= blocks ---
    i = 0
    while i < len(lines):
        m = re.match(r'copy\s*=\s*"?([^",\n]+)"?', lines[i].strip(), re.IGNORECASE)
        if m:
            block_name = m.group(1).strip()
            # strip trailing copy-source like ",Xiage"
            block_name = re.sub(r',.*$', '', block_name).strip()
            # strip trailing _Raid / Raid suffix to get just the label
            label_part = re.sub(r'^[^_]+_', '', block_name)  # "Xiage_Raid" → "Raid"
            canonical = _canonical(label_part) or _canonical(block_name)
            if canonical:
                for j in range(i + 1, min(i + 30, len(lines))):
                    if re.match(r'copy\s*=', lines[j].strip(), re.IGNORECASE):
                        break
                    tm = re.match(r'#?\s*talents\s*=\s*(\S+)', lines[j].strip())
                    if tm:
                        result[canonical] = tm.group(1)
                        break
        i += 1

    # --- Format 2: section-header comments ---
    if not result:
        pending: str | None = None
        for line in lines:
            s = line.strip()
            # Header: ### Raid  or  # --- ST ---  or  ## Single Target
            hm = re.match(r'^#{1,3}[-\s]*([A-Za-z][^\n]*)[-\s]*$', s)
            if hm:
                pending = _canonical(hm.group(1).strip())
                continue
            if pending:
                tm = re.match(r'#?\s*talents\s*=\s*(\S+)', s)
                if tm:
                    result[pending] = tm.group(1)
                    pending = None
                elif s and not s.startswith('#'):
                    pending = None  # non-comment non-empty line = end of section

    return result


def apply_talent(simc: str, talent_code: str) -> str:
    """Replace the active talents= line in a SimC string with talent_code."""
    return re.sub(r'^talents\s*=\s*\S+', f'talents={talent_code}', simc, flags=re.MULTILINE)

CONFIG_PATH = Path(__file__).parent / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "droptimizer.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Raidbots endpoints
# ---------------------------------------------------------------------------

RAIDBOTS_BASE    = "https://www.raidbots.com"
SUBMIT_URL       = RAIDBOTS_BASE + "/sim"
STATUS_URL_TMPL  = RAIDBOTS_BASE + "/api/job/{job_id}"
REPORT_URL_TMPL  = RAIDBOTS_BASE + "/simbot/report/{job_id}"
WOWAPI_CHAR_TMPL = RAIDBOTS_BASE + "/wowapi/character/{region}/{realm}/{name}"

RAIDBOTS_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 OPR/127.0.0.0",
    "Referer":      "https://www.raidbots.com/simbot/droptimizer",
    "Origin":       "https://www.raidbots.com",
}

from payload_builder import (
    CharacterIdentity, SimTarget, StaticData,
    build_payload, DIFFICULTY_MAP, VIRTUAL_INSTANCES,
)

# ---------------------------------------------------------------------------
# Discord bot API
# ---------------------------------------------------------------------------

DISCORD_API      = "https://discord.com/api/v10"
DISCORD_DM_OPEN  = DISCORD_API + "/users/@me/channels"
DISCORD_MSG_TMPL = DISCORD_API + "/channels/{channel_id}/messages"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.json not found at %s", CONFIG_PATH)
        sys.exit(1)
    with CONFIG_PATH.open() as f:
        return json.load(f)


def get_site_versions(session: requests.Session) -> tuple[str, str]:
    """
    Extract the static data hash and frontend version from the Raidbots page.
    Returns (static_hash, frontend_version).
    """
    FALLBACK_HASH    = "9de61c8c43f6275a761d44bf4683b542"
    FALLBACK_FRONTEND = "76b791ae3944c21fb3d4"
    try:
        page = session.get(RAIDBOTS_BASE + "/simbot/droptimizer", timeout=15)
        # Both values are embedded in the inline initialData script block
        hash_match     = re.search(r'"gameDataVersion"\s*:\s*"([a-f0-9]{32})"', page.text)
        frontend_match = re.search(r'"initialVersion"\s*:\s*"([a-f0-9]+)"', page.text)
        static_hash    = hash_match.group(1)     if hash_match     else FALLBACK_HASH
        frontend_ver   = frontend_match.group(1) if frontend_match else FALLBACK_FRONTEND
        return static_hash, frontend_ver
    except Exception as e:
        log.warning("Could not auto-detect site versions: %s", e)
    return FALLBACK_HASH, FALLBACK_FRONTEND


def fetch_character(session: requests.Session, region: str, realm: str, name: str) -> dict:
    url = WOWAPI_CHAR_TMPL.format(region=region, realm=realm, name=name)
    log.info("Fetching character data for %s/%s/%s ...", region, realm, name)
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_encounter_items(session: requests.Session, static_hash: str) -> list:
    url = f"{RAIDBOTS_BASE}/static/data/{static_hash}/encounter-items.json"
    log.info("Fetching encounter items database...")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_static_data(session: requests.Session) -> StaticData:
    """Fetch all Raidbots static data needed by build_payload in one call.

    Returns a :class:`~payload_builder.StaticData` instance containing
    encounter items, instances, and frontend version — ready to pass
    directly to :func:`~payload_builder.build_payload`.
    """
    static_hash, frontend_version = get_site_versions(session)
    encounter_items = fetch_encounter_items(session, static_hash)
    instances_resp = session.get(
        f"{RAIDBOTS_BASE}/static/data/{static_hash}/instances.json", timeout=15
    )
    instances = instances_resp.json()
    return StaticData(
        encounter_items=encounter_items,
        instances=instances,
        frontend_version=frontend_version,
    )


def submit_job(session: requests.Session, payload: dict, api_key: str | None) -> tuple[str, str]:
    headers = dict(RAIDBOTS_HEADERS)
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    diff    = payload["droptimizer"].get("difficulty", "?")
    inst    = payload["droptimizer"].get("instance", "?")
    upgrade = payload["droptimizer"].get("upgradeLevel", "?")
    log.info("Submitting Droptimizer — instance %s %s (upgradeLevel %s) ...", inst, diff, upgrade)
    log.info("Payload size: %d bytes", len(json.dumps(payload)))

    # Dump payload to file for debugging
    dump_path = Path(__file__).parent / "payload_debug.json"
    with open(dump_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("Payload dumped to %s", dump_path)

    _RETRYABLE = {429, 502, 503, 504}
    _DELAYS    = [5, 15, 30]

    resp = None
    for attempt, delay in enumerate([0] + _DELAYS):
        if delay:
            log.warning("Retrying submission in %ds (attempt %d/3)...", delay, attempt)
            time.sleep(delay)
        resp = session.post(SUBMIT_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code not in _RETRYABLE:
            break
        log.warning("Raidbots returned %s — will retry.", resp.status_code)
    else:
        log.error("Raidbots still returned %s after %d retries: %s",
                  resp.status_code, len(_DELAYS), resp.text[:300])
        resp.raise_for_status()

    if not resp.ok:
        log.error("Raidbots error %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()

    data   = resp.json()
    # simId (alphanumeric) is used for both the poll endpoint and report URL.
    # jobId (numeric) is not used.
    sim_id = data.get("simId") or data.get("job", {}).get("id") or data.get("id")
    if not sim_id:
        log.error("Unexpected response from Raidbots: %s", data)
        sys.exit(1)

    log.info("Job submitted — simId: %s", sim_id)
    return sim_id, sim_id


def poll_job(session: requests.Session, job_id: str, timeout_minutes: int = 30) -> bool:
    # Raidbots removes jobs from /api/job/ once complete, so poll the report
    # data endpoint instead — 200 means done, 404 means still running.
    report_url = RAIDBOTS_BASE + f"/simbot/report/{job_id}/data.json"
    job_url    = STATUS_URL_TMPL.format(job_id=job_id)
    deadline   = time.time() + timeout_minutes * 60
    interval   = 15

    log.info("Polling job %s (up to %d min) ...", job_id, timeout_minutes)
    while time.time() < deadline:
        # First check if the report is already available
        try:
            r = session.get(report_url, timeout=15)
            if r.status_code == 200:
                log.info("  Report ready.")
                return True
        except requests.RequestException:
            pass

        # Fall back to job status endpoint (present while job is queued/running)
        try:
            r = session.get(job_url, timeout=15)
            if r.ok:
                data   = r.json()
                job    = data.get("job", data)
                status = job.get("state") or job.get("status", "")
                log.info("  status: %s", status)
                if status in ("error", "failed", "cancelled"):
                    log.error("Job ended with status: %s", status)
                    return False
        except requests.RequestException as exc:
            log.warning("Poll error: %s", exc)

        time.sleep(interval)
        interval = min(interval + 5, 60)

    log.error("Timed out waiting for job %s", job_id)
    return False


def discord_auth_headers(bot_token: str) -> dict:
    return {
        "Authorization": "Bot " + bot_token,
        "Content-Type":  "application/json",
        "User-Agent":    "droptimizer-daily-bot/1.0",
    }


def open_dm_channel(bot_token: str, target_user_id: str) -> str:
    resp = requests.post(
        DISCORD_DM_OPEN,
        json={"recipient_id": target_user_id},
        headers=discord_auth_headers(bot_token),
        timeout=15,
    )
    resp.raise_for_status()
    channel_id = resp.json()["id"]
    log.info("DM channel ready — channel ID: %s", channel_id)
    return channel_id


def send_dm(bot_token: str, channel_id: str, message: str) -> None:
    url  = DISCORD_MSG_TMPL.format(channel_id=channel_id)
    resp = requests.post(
        url,
        json={"content": message},
        headers=discord_auth_headers(bot_token),
        timeout=15,
    )
    resp.raise_for_status()
    log.info("DM sent: %s", message)


def notify_discord(
    bot_token: str,
    channel_id: str,
    results: list[dict],
    notify_on_failure: bool,
) -> None:
    for r in results:
        if r["success"]:
            report_url = REPORT_URL_TMPL.format(job_id=r["job_id"])
            send_dm(bot_token, channel_id, "/wishlist " + report_url)
        elif notify_on_failure:
            send_dm(
                bot_token, channel_id,
                "Droptimizer sim failed for {} {} — check droptimizer.log".format(
                    r.get("instance"), r.get("difficulty")
                ),
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()

    bot_token = cfg.get("discord_bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        log.error("discord_bot_token is missing or not set in config.json")
        sys.exit(1)

    discord_channel_id = cfg.get("discord_channel_id")
    if not discord_channel_id:
        log.error("discord_channel_id is missing from config.json")
        sys.exit(1)

    simc = cfg.get("simc_string")
    if not simc:
        log.error("simc_string is missing from config.json")
        sys.exit(1)

    raidsid           = cfg.get("raidsid", "")
    api_key           = cfg.get("raidbots_api_key")
    timeout           = cfg.get("timeout_minutes", 30)
    notify_on_failure = cfg.get("notify_on_failure", True)
    char_cfg          = cfg["character"]

    runs = cfg.get("runs")
    if not runs:
        log.error("No 'runs' defined in config.json")
        sys.exit(1)

    from raidbots_session import make_raidbots_session
    session   = make_raidbots_session(raidsid)
    static    = fetch_static_data(session)
    character = fetch_character(session, char_cfg["region"], char_cfg["realm"], char_cfg["name"])

    results = []
    for run in runs:
        identity = CharacterIdentity(
            name=char_cfg["name"],
            realm=char_cfg["realm"],
            region=char_cfg["region"],
            spec_label=run.get("spec", "Fire"),
            simc_string=simc,
        )
        target = SimTarget(
            difficulty=run.get("difficulty", "raid-heroic"),
            instance_id=run.get("instance_id", -91),
            spec_id=run.get("spec_id", 63),
            loot_spec_id=run.get("loot_spec_id", run.get("spec_id", 63)),
            fight_style=run.get("fight_style", "Patchwerk"),
            iterations=run.get("iterations", "smart"),
            crafted_stats=run.get("crafted_stats", "36/49"),
        )
        payload         = build_payload(identity, target, character, static)
        job_id, sim_id  = submit_job(session, payload, api_key)
        success         = poll_job(session, job_id, timeout_minutes=timeout)

        results.append({
            "job_id":     sim_id,
            "instance":   run.get("instance_id", -91),
            "difficulty": run.get("difficulty"),
            "success":    success,
        })

        if not success:
            log.warning("Run for %s/%s did not complete.", run.get("instance_id"), run.get("difficulty"))

    notify_discord(bot_token, discord_channel_id, results, notify_on_failure)

    failed = [r for r in results if not r["success"]]
    if failed:
        log.error("%d run(s) failed.", len(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()