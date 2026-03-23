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

# Difficulty string -> (upgradeLevel bonus ID for 6/6, levelSelectorSequence)
# Hero track  = raid-heroic, group 611, 6/6 bonusId 12798
# Myth track  = raid-mythic, group 612, 6/6 bonusId 12806
DIFFICULTY_MAP = {
    "raid-heroic": {"upgradeLevel": 12798, "levelSelectorSequence": 611, "itemLevel": "Hero",  "season": "mid1"},
    "raid-mythic":  {"upgradeLevel": 12806, "levelSelectorSequence": 612, "itemLevel": "Myth",  "season": "mid1"},
}

# Virtual instance ID -> real raid instance IDs it aggregates.
# -91 = TWW Season 1 Raids (all three raid instances combined).
VIRTUAL_INSTANCES: dict[int, list[int]] = {
    -91: [1307, 1308, 1314],
}

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


def get_slot_name(inventory_type: int) -> str | None:
    """Map WoW inventoryType to Raidbots slot name."""
    return {
        1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest",
        6: "waist", 7: "legs", 8: "feet", 9: "wrist", 10: "hands",
        11: "finger1", 12: "trinket1", 13: "trinket1", 14: "back",
        15: "main_hand", 16: "back", 17: "main_hand", 20: "chest",
        21: "main_hand", 22: "off_hand", 23: "off_hand", 28: "ranged",
    }.get(inventory_type)


def build_droptimizer_items(
    encounter_items: list,
    instance_data: dict,
    difficulty: str,
    character: dict,
    upgrade_info: dict,
    all_instances: list,
) -> list:
    """
    Build the droptimizerItems array by filtering encounter items
    for the given instance and applying upgrade bonus IDs.
    """
    upgrade_bonus_id    = upgrade_info["upgradeLevel"]
    level_selector_seq  = upgrade_info["levelSelectorSequence"]
    item_level_name     = upgrade_info["itemLevel"]
    season              = upgrade_info["season"]
    class_id            = character.get("class", 8)

    # Virtual instances like -91 (Season 1 Raids) aggregate encounters from
    # multiple real raid instances. If the virtual instance has no direct
    # encounters, expand it using the VIRTUAL_INSTANCES mapping.
    virtual_instance_id = instance_data["id"]
    enc_list = instance_data.get("encounters", [])
    if not enc_list and virtual_instance_id in VIRTUAL_INSTANCES:
        sub_ids = set(VIRTUAL_INSTANCES[virtual_instance_id])
        enc_list = [
            enc
            for inst in all_instances
            if inst["id"] in sub_ids
            for enc in inst.get("encounters", [])
        ]
    virtual_encounter_ids   = {e["id"] for e in enc_list}
    virtual_encounter_order = {e["id"]: i for i, e in enumerate(enc_list)}

    # Map encounterId -> real instanceId from non-virtual instances
    encounter_to_real_instance: dict[int, int] = {}
    for inst in all_instances:
        if inst["id"] < 0:
            continue
        for enc in inst.get("encounters", []):
            if enc["id"] in virtual_encounter_ids:
                encounter_to_real_instance[enc["id"]] = inst["id"]

    log.info(
        "Resolved %d encounters across real instances: %s",
        len(encounter_to_real_instance),
        sorted(set(encounter_to_real_instance.values())),
    )

    result = []

    for item in encounter_items:
        sources = item.get("sources", [])
        item_class = item.get("itemClass")
        inv_type   = item.get("inventoryType")

        # Skip non-gear
        if item_class not in (2, 4):
            continue
        slot = get_slot_name(inv_type)
        if not slot:
            continue

        # Check if item comes from any encounter in this virtual instance
        matching_sources = [
            s for s in sources
            if s.get("encounterId") in virtual_encounter_ids
        ]
        if not matching_sources:
            continue

        # Check class restrictions
        allowed_classes = item.get("allowableClasses")
        if allowed_classes and class_id not in allowed_classes:
            continue

        # Emit one entry per source encounter
        seen = set()
        for src in matching_sources:
            enc_id = src["encounterId"]
            key = (item["id"], enc_id, slot)
            if key in seen:
                continue
            seen.add(key)

            seq_offset  = virtual_encounter_order.get(enc_id, 0)
            real_inst_id = encounter_to_real_instance.get(enc_id, virtual_instance_id)

            # Apply upgrade bonus IDs: [quality_bonus, expansion_bonus, upgrade_level_bonus]
            bonus_lists = [4799, 4786, upgrade_bonus_id]
            socket_info = item.get("socketInfo", {})
            has_socket = (
                isinstance(socket_info, dict) and
                any(isinstance(v, dict) and v.get("staticSlots", 0) > 0
                    for v in socket_info.values())
            )
            if has_socket:
                bonus_lists = [13668] + bonus_lists

            enchant_id = 0
            # Carry forward the player's current enchant if same slot
            equipped = character.get("items", {})
            for eq_slot, eq_item in equipped.items():
                if not isinstance(eq_item, dict):
                    continue
                if get_slot_name(eq_item.get("inventoryType", 0)) == slot:
                    enchant_id = eq_item.get("enchant_id") or 0
                    break

            # Find the real instance and encounter objects for embedding
            real_instance_obj = next(
                (i for i in all_instances if i["id"] == real_inst_id), {"id": real_inst_id}
            )
            encounter_obj = next(
                (e for i in all_instances if i["id"] == real_inst_id
                 for e in i.get("encounters", []) if e["id"] == enc_id),
                {"id": enc_id}
            )

            entry = {
                "id": f"{real_inst_id}/{enc_id}/{difficulty}/{item['id']}/{item.get('itemLevel', 276)}/{enchant_id}/{slot}///",
                "slot": slot,
                "item": {
                    **{k: v for k, v in item.items() if k not in ("sources",)},
                    "bonusLists":   bonus_lists,
                    "bonus_id":     "/".join(str(b) for b in bonus_lists),
                    "enchant_id":   enchant_id,
                    "gem_id":       "",
                    "instanceId":   real_inst_id,
                    "encounterId":  enc_id,
                    "difficulty":   difficulty,
                    "offSpecItem":  False,
                    "upgrade": {
                        "group":    level_selector_seq,
                        "level":    6,
                        "max":      6,
                        "name":     item_level_name,
                        "fullName": f"{item_level_name} 6/6",
                        "bonusId":  upgrade_bonus_id,
                        "itemLevel": item.get("itemLevel", 276),
                        "seasonId": 34,
                    },
                    "instance": real_instance_obj,
                    "encounter": encounter_obj,
                    "overrides": {
                        "encounterId":               enc_id,
                        "encounterSequenceOffset":   seq_offset,
                        "instanceId":                real_inst_id,
                        "difficulty":                difficulty,
                        "itemLevel":                 item_level_name,
                        "levelSelectorSequence":     level_selector_seq,
                        "season":                    season,
                        "levelSelectorSetUpgradeTrack": True,
                        "seasonId":                  34,
                        "disableWarforgeLevel":      True,
                        "enableSockets":             True,
                        "itemConversion":            {"id": 12, "minLevel": 220},
                        "instance":                  real_instance_obj,
                        "encounter":                 encounter_obj,
                        "encounterType":             "boss",
                        "encounterTypePlural":       "bosses",
                        "quality":                   4,
                    },
                    "socketInfo": item.get("socketInfo", {}),
                    "tooltipParams": {"enchant": enchant_id},
                },
            }
            result.append(entry)

    log.info("Built %d droptimizerItems for instance %s %s", len(result), virtual_instance_id, difficulty)
    return result


def build_payload(
    cfg: dict,
    run: dict,
    character: dict,
    encounter_items: list,
    instances: list,
    frontend_version: str = "76b791ae3944c21fb3d4",
) -> dict:
    char    = cfg["character"]
    diff_key = run.get("difficulty", "raid-heroic")  # e.g. "raid-heroic"
    instance_numeric = run.get("instance_id", -91)

    upgrade_info = DIFFICULTY_MAP.get(diff_key, DIFFICULTY_MAP["raid-heroic"])

    # Find the instance data
    instance_data = next(
        (i for i in instances if i["id"] == instance_numeric), {}
    )

    droptimizer_items = build_droptimizer_items(
        encounter_items, instance_data, diff_key, character, upgrade_info, instances
    )

    spec_id      = run.get("spec_id", 63)
    loot_spec_id = run.get("loot_spec_id", spec_id)
    class_id     = character.get("class", 8)
    faction      = "alliance" if character.get("faction", 0) == 0 else "horde"

    # Strip None and non-dict entries from items objects — Raidbots backend
    # crashes (500) when it encounters nil slots like offHand/shirt/tabard.
    clean_items = {k: v for k, v in character.get("items", {}).items() if isinstance(v, dict)}
    character = {**character, "items": clean_items}

    report_name = (
        f"Droptimizer • Season 1 Raids • "
        f"{'Heroic' if diff_key == 'raid-heroic' else 'Mythic'} • "
        f"{upgrade_info['itemLevel']} 6/6"
    )

    payload = {
        "type":             "droptimizer",
        "text":             cfg.get("simc_string", ""),
        "baseActorName":    char["name"],
        "spec":             run.get("spec", "Fire"),
        "armory":           {"region": char["region"], "realm": char["realm"], "name": char["name"]},
        "character":        character,
        "reportName":       report_name,
        "frontendHost":     "www.raidbots.com",
        "frontendVersion":  frontend_version,
        "iterations":       run.get("iterations", "smart"),
        "fightStyle":       run.get("fight_style", "Patchwerk"),
        "fightLength":      300,
        "enemyCount":       1,
        "enemyType":        "FluffyPillow",
        "bloodlust":        True,
        "arcaneIntellect":  True,
        "fortitude":        True,
        "battleShout":      True,
        "mysticTouch":      True,
        "chaosBrand":       True,
        "markOfTheWild":    True,
        "skyfury":          True,
        "bleeding":         True,
        "reportDetails":    True,
        "ptr":              False,
        "simcVersion":      "weekly",
        # Legacy/optional sim toggles — required by Raidbots backend
        "aberration":                   False,
        "apl":                          "",
        "astralAntennaMissChance":      10,
        "attunedToTheAether":           False,
        "augmentation":                 "",
        "balefireBranchRngType":        "constant",
        "blueSilkenLining":             40,
        "cabalistsHymnalInParty":       0,
        "corruptingRageUptime":         80,
        "covenantChance":               100,
        "cruciblePredation":            True,
        "crucibleSustenance":           True,
        "crucibleViolence":             True,
        "dawnDuskThreadLining":         100,
        "disableIqdExecute":            False,
        "email":                        "",
        "enableDominationShards":       False,
        "enableRuneWords":              False,
        "essenceGorgerHighStat":        False,
        "flask":                        "",
        "food":                         "",
        "frontendVersion":              "aa117406d3c58c9dc83a0df039513166f66a640a",
        "gearsets":                     [],
        "huntersMark":                  True,
        "iqdStatFailChance":            0,
        "loyalToTheEndAllies":          0,
        "nazjatar":                     False,
        "nyalotha":                     True,
        "ocularGlandUptime":            100,
        "ominousChromaticEssenceAllies":   "",
        "ominousChromaticEssencePersonal": "obsidian",
        "potion":                       "",
        "powerInfusion":                False,
        "primalRitualShell":            "wind",
        "rubyWhelpShellTraining":       "",
        "sendEmail":                    False,
        "smartAggressive":              False,
        "smartHighPrecision":           True,
        "soleahStatType":               "haste",
        "stoneLegionHeraldryInParty":   0,
        "surgingVitality":              0,
        "symbioticPresence":            22,
        "talentSets":                   [],
        "talents":                      None,
        "temporaryEnchant":             "",
        "unboundChangelingStatType":    "",
        "undulatingTides":              100,
        "voidRitual":                   False,
        "whisperingIncarnateIconRoles": "dps/heal/tank",
        "worldveinAllies":              0,
        "droptimizer": {
            "equipped":             character.get("items", {}),
            "instance":             instance_numeric,
            "difficulty":           diff_key,
            "warforgeLevel":        0,
            "upgradeLevel":         upgrade_info["upgradeLevel"],
            "upgradeEquipped":      False,
            "gem":                  None,
            "classId":              class_id,
            "specId":               spec_id,
            "lootSpecId":           loot_spec_id,
            "faction":              faction,
            "craftedStats":         run.get("crafted_stats", "36/49"),
            "offSpecItems":         False,
            "includeConversions":   True,
        },
        "droptimizerItems": droptimizer_items,
        "simOptions": {
            "fightstyle": run.get("fight_style", "Patchwerk"),
            "iterations": run.get("iterations", "smart"),
        },
    }
    return payload


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

    resp = session.post(SUBMIT_URL, json=payload, headers=headers, timeout=60)

    if resp.status_code == 429:
        log.error("Rate-limited by Raidbots (429). Try again later.")
        sys.exit(1)

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

    # Build a shared session with cookies
    session = requests.Session()
    session.headers.update(RAIDBOTS_HEADERS)
    if raidsid:
        session.cookies.set("raidsid", raidsid, domain="www.raidbots.com")

    # Fetch static data once
    static_hash, frontend_version = get_site_versions(session)
    log.info("Using static data hash: %s  frontend: %s", static_hash, frontend_version)
    character       = fetch_character(session, char_cfg["region"], char_cfg["realm"], char_cfg["name"])
    encounter_items = fetch_encounter_items(session, static_hash)

    # Fetch instances list
    instances_resp = session.get(
        f"{RAIDBOTS_BASE}/static/data/{static_hash}/instances.json", timeout=15
    )
    instances = instances_resp.json()

    results = []
    for run in runs:
        payload         = build_payload(cfg, run, character, encounter_items, instances, frontend_version)
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