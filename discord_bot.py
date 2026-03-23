"""discord_bot.py — Discord bot with /droptimizer slash command."""

import asyncio
import json
from pathlib import Path

import discord
from discord import app_commands

from simulation_runner import RunnerConfig, SimulationRunner

CONFIG_PATH = Path(__file__).parent / "config.json"
CHARS_PATH  = Path(__file__).parent / "characters.json"


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


def _parse_char_label(simc: str) -> str:
    """Return a short 'Name – Spec' label from the first lines of a SimC string."""
    from simulation_runner import parse_simc
    info = parse_simc(simc)
    return f"{info.get('name', '?')} \u2013 {info.get('spec', '?').capitalize()}"


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
        char_label = _parse_char_label(simc)
    except Exception as e:
        await interaction.followup.send(f"Could not read SimC input: {e}", ephemeral=True)
        return

    await interaction.followup.send(
        f"Running sims for **{char_label}**\u2026 I\u2019ll DM you the results when done.",
        ephemeral=True,
    )

    cfg     = _load_config()
    config  = RunnerConfig(raidsid=cfg.get("raidsid", ""))
    runner  = SimulationRunner(config, chars_path=CHARS_PATH)

    try:
        results = await runner.run_async(simc)
    except Exception as e:
        try:
            await interaction.user.send(f"Droptimizer failed for **{char_label}**: {e}")
        except discord.Forbidden:
            pass
        return

    lines = [f"**Droptimizer results \u2014 {char_label}**\n"]
    for r in sorted(results, key=lambda x: x["label"]):
        status = "\u2705" if r["ok"] else "\u274c"
        lines.append(f"{status} **{r['label']}** \u2014 {r['url']}")

    message = "\n".join(lines)
    try:
        await interaction.user.send(message)
    except discord.Forbidden:
        await interaction.followup.send(
            "Couldn\u2019t DM you \u2014 please enable DMs from server members.\n\n" + message,
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Entry point (called from app.py in a thread)
# ---------------------------------------------------------------------------

def start(token: str) -> None:
    """Run the bot in a dedicated asyncio event loop (blocking)."""
    asyncio.run(bot.start(token))
