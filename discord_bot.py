"""discord_bot.py — Discord bot with /droptimizer slash command."""

import asyncio
import json
import re
import concurrent.futures
from pathlib import Path

import discord
from discord import app_commands
import requests as req_lib

from droptimizer import (
    RAIDBOTS_BASE, RAIDBOTS_HEADERS,
    apply_talent, build_payload, find_talent_builds,
    fetch_character, fetch_encounter_items,
    get_site_versions, poll_job, submit_job,
)
from qe_sim import is_healer, run_qe_upgradefinder

CONFIG_PATH = Path(__file__).parent / "config.json"
CHARS_PATH  = Path(__file__).parent / "characters.json"
REPORT_URL  = RAIDBOTS_BASE + "/simbot/report/{sim_id}"

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _load_characters() -> list:
    if CHARS_PATH.exists():
        try:
            return json.loads(CHARS_PATH.read_text())
        except Exception:
            pass
    return []


def _parse_simc(simc: str) -> dict:
    """Extract name, char_class, region, realm, spec from a SimC string."""
    result: dict = {}
    for line in simc.splitlines():
        m = re.match(r'^([\w][\w\s]*)="([^"]+)"', line)
        if m and "char_class" not in result:
            result["char_class"] = m.group(1).strip().lower().replace(" ", "_")
            result["name"] = m.group(2).strip()
        kv = re.match(r'^(\w+)\s*=\s*(.+)', line)
        if not kv:
            continue
        k, v = kv.group(1), kv.group(2).strip()
        if k == "region": result["region"] = v.lower()
        if k == "server": result["realm"]  = v.lower()
        if k == "spec":   result["spec"]   = v.lower()
    return result


def _make_session(raidsid: str) -> req_lib.Session:
    s = req_lib.Session()
    s.headers.update(RAIDBOTS_HEADERS)
    if raidsid:
        s.cookies.set("raidsid", raidsid, domain="www.raidbots.com")
    return s


def _run_sims(simc: str, raidsid: str) -> list[dict]:
    """
    Fetch static data, resolve character presets, then run all talent build ×
    difficulty combinations in parallel.  Returns a list of result dicts.
    """
    info = _parse_simc(simc)
    if not all(k in info for k in ("name", "region", "realm", "spec")):
        raise ValueError("Could not parse name / region / realm / spec from SimC string.")

    # Resolve presets from saved characters (same name + spec)
    saved = {
        (c["name"].lower(), c["spec"].lower()): c
        for c in _load_characters()
    }
    preset = saved.get((info["name"].lower(), info["spec"].lower()))
    spec_id       = (preset.get("spec_id")      if preset else None) or \
                    SPEC_IDS.get(info.get("char_class", ""), {}).get(info["spec"], 63)
    loot_spec_id  = (preset.get("loot_spec_id") if preset else None) or spec_id
    crafted_stats = (preset.get("crafted_stats") if preset else None) or "36/49"
    simc_final    = (preset.get("simc_string")   if preset else None) or simc

    # ── Healer: use QE Upgrade Finder ───────────────────────────────────────
    if is_healer(spec_id):
        talent_builds = find_talent_builds(simc_final) or {"": None}
        results = []

        def _qe_one(build_label: str, talent_code: str | None) -> dict:
            sim_simc = apply_talent(simc_final, talent_code) if talent_code else simc_final
            label = f"Heroic + Mythic{' – ' + build_label if build_label else ''}"
            try:
                url = run_qe_upgradefinder(sim_simc)
                return {"label": label, "url": url, "ok": True}
            except Exception as e:
                return {"label": label, "url": "", "ok": False, "error": str(e)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(talent_builds)) as pool:
            futures = [pool.submit(_qe_one, bl, tc) for bl, tc in talent_builds.items()]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        return results

    # ── DPS / Tank: use Raidbots Droptimizer ────────────────────────────────
    init_session = _make_session(raidsid)
    static_hash, frontend_version = get_site_versions(init_session)
    encounter_items = fetch_encounter_items(init_session, static_hash)
    instances = init_session.get(
        f"{RAIDBOTS_BASE}/static/data/{static_hash}/instances.json", timeout=15
    ).json()
    character = fetch_character(init_session, info["region"], info["realm"], info["name"])

    # Build job list
    talent_builds = find_talent_builds(simc_final) or {"": None}
    jobs = [
        (build_label, talent_code, difficulty)
        for build_label, talent_code in talent_builds.items()
        for difficulty in ("raid-heroic", "raid-mythic")
    ]

    def _one(build_label: str, talent_code: str | None, difficulty: str) -> dict:
        s = _make_session(raidsid)
        sim_simc = apply_talent(simc_final, talent_code) if talent_code else simc_final
        cfg_wrap = {
            "character":   {"name": info["name"], "realm": info["realm"], "region": info["region"]},
            "simc_string": sim_simc,
        }
        run_opts = {
            "difficulty":    difficulty,
            "instance_id":   -91,
            "spec":          info["spec"].capitalize(),
            "spec_id":       spec_id,
            "loot_spec_id":  loot_spec_id,
            "fight_style":   "Patchwerk",
            "iterations":    "smart",
            "crafted_stats": crafted_stats,
        }
        payload   = build_payload(cfg_wrap, run_opts, character, encounter_items, instances, frontend_version)
        sim_id, _ = submit_job(s, payload, None)
        ok        = poll_job(s, sim_id, timeout_minutes=30)
        diff_name = "Heroic" if difficulty == "raid-heroic" else "Mythic"
        label     = f"{diff_name}{' – ' + build_label if build_label else ''}"
        return {"label": label, "url": REPORT_URL.format(sim_id=sim_id), "ok": ok}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(_one, *j) for j in jobs]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    return results


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot     = discord.Client(intents=intents)
tree    = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"[discord] Logged in as {bot.user} — slash commands synced.")


@tree.command(name="droptimizer", description="Run Heroic + Mythic droptimizer sims from a SimC string or file")
@app_commands.describe(
    simc_string="Paste your SimC string directly here",
    simc_file="Or attach a .txt SimC export file",
)
async def droptimizer_cmd(
    interaction: discord.Interaction,
    simc_string: str | None = None,
    simc_file: discord.Attachment | None = None,
):
    if not simc_string and not simc_file:
        await interaction.response.send_message(
            "Please provide a SimC string or attach a `.txt` file.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        if simc_file:
            raw  = await simc_file.read()
            simc = raw.decode("utf-8")
        else:
            simc = simc_string
        info  = _parse_simc(simc)
        char_label = f"{info.get('name', '?')} – {info.get('spec', '?').capitalize()}"
    except Exception as e:
        await interaction.followup.send(f"Could not read SimC input: {e}", ephemeral=True)
        return

    await interaction.followup.send(
        f"Running sims for **{char_label}**… I'll DM you the results when done.",
        ephemeral=True,
    )

    cfg     = _load_config()
    raidsid = cfg.get("raidsid", "")

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(None, _run_sims, simc, raidsid)
    except Exception as e:
        try:
            await interaction.user.send(f"Droptimizer failed for **{char_label}**: {e}")
        except discord.Forbidden:
            pass
        return

    lines = [f"**Droptimizer results — {char_label}**\n"]
    for r in sorted(results, key=lambda x: x["label"]):
        status = "✅" if r["ok"] else "❌"
        lines.append(f"{status} **{r['label']}** — {r['url']}")

    message = "\n".join(lines)
    try:
        await interaction.user.send(message)
    except discord.Forbidden:
        await interaction.followup.send(
            "Couldn't DM you — please enable DMs from server members.\n\n" + message,
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Entry point (called from app.py in a thread)
# ---------------------------------------------------------------------------

def start(token: str) -> None:
    """Run the bot in a dedicated asyncio event loop (blocking)."""
    asyncio.run(bot.start(token))
