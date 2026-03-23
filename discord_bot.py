"""discord_bot.py — Discord bot with /droptimizer slash command."""

import asyncio
import json
import re
import concurrent.futures
from pathlib import Path

import discord
from discord import app_commands

from droptimizer import (
    RAIDBOTS_BASE,
    apply_talent, fetch_character, fetch_static_data, find_talent_builds,
)
from payload_builder import CharacterIdentity, SimTarget
from raidbots_session import make_raidbots_session
from sim_router import is_healer, run_qe_sim, run_raidbots_sim

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
            r = run_qe_sim(sim_simc, label=label)
            return {"label": r.label, "url": r.url, "ok": r.ok, "error": r.error}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(talent_builds)) as pool:
            futures = [pool.submit(_qe_one, bl, tc) for bl, tc in talent_builds.items()]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        return results

    # ── DPS / Tank: use Raidbots Droptimizer ────────────────────────────────
    init_session = make_raidbots_session(raidsid)
    static    = fetch_static_data(init_session)
    character = fetch_character(init_session, info["region"], info["realm"], info["name"])

    # Build job list
    talent_builds = find_talent_builds(simc_final) or {"": None}
    jobs = [
        (build_label, talent_code, difficulty)
        for build_label, talent_code in talent_builds.items()
        for difficulty in ("raid-heroic", "raid-mythic")
    ]

    def _one(build_label: str, talent_code: str | None, difficulty: str) -> dict:
        s = make_raidbots_session(raidsid)
        sim_simc = apply_talent(simc_final, talent_code) if talent_code else simc_final
        diff_name = "Heroic" if difficulty == "raid-heroic" else "Mythic"
        label     = f"{diff_name}{' – ' + build_label if build_label else ''}"
        identity = CharacterIdentity(
            name=info["name"], realm=info["realm"], region=info["region"],
            spec_label=info["spec"].capitalize(), simc_string=sim_simc,
        )
        target = SimTarget(
            difficulty=difficulty,
            spec_id=spec_id,
            loot_spec_id=loot_spec_id,
            crafted_stats=crafted_stats,
        )
        r = run_raidbots_sim(s, identity, target, character, static,
                             report_url_template=REPORT_URL)
        return {"label": label, "url": r.url, "ok": r.ok}

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
