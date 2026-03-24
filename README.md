# Auto Sim

Automates World of Warcraft gear-optimization simulations for multiple characters and specs, posts results to Discord.

- **DPS / Tank specs** → [Raidbots](https://www.raidbots.com) Droptimizer API
- **Healer specs** → [QuestionablyEpic](https://questionablyepic.com) Upgrade Finder (browser automation via Playwright)

---

## How it works

```
Web UI  ──or──  Discord /droptimizer
        │
        ▼
  simulation_runner.py   (orchestration, parallel jobs)
        │
        ▼
   sim_router.py
   ├── DPS/Tank spec? ──► droptimizer.py ──► Raidbots API ──► poll ──► report URL
   └── Healer spec?   ──► qe_sim.py      ──► QE browser automation ──► report URL
        │
        ▼
  Discord bot posts results to configured channel
```

All jobs run in parallel via a thread-pool. Results (URLs + status) are persisted to `last_run.json` so they survive a restart.

---

## Features

- **Web UI** — dark-themed single-page interface for selecting characters, difficulties, and launching runs
- **Discord bot** — `/droptimizer` slash command; results posted as DMs from the bot
- **Smart routing** — automatically sends healer specs to QE, everything else to Raidbots
- **Grouped character list** — characters with multiple specs are collapsed into expandable rows
- **Multiple difficulties per run** — tick any combination of Normal / Heroic / Mythic (raids) or M+7 / M+10 / M+10 Vault (dungeons)
- **Saved character presets** — store SimC strings + spec configurations in `characters.json`
- **Real-time status** — web UI polls `/api/status` and shows per-job badges (pending → fetching → submitting → running → done / error)

### Supported difficulties

| Key | Content | Fight style |
|---|---|---|
| `raid-normal` | Normal raid (Champion track) | Patchwerk |
| `raid-heroic` | Heroic raid (Hero track) | Patchwerk |
| `raid-mythic` | Mythic raid (Myth track) | Patchwerk |
| `dungeon-mythic-7` | M+7 end-of-dungeon loot | DungeonSlice |
| `dungeon-mythic-10` | M+10 end-of-dungeon loot | DungeonSlice |
| `dungeon-mythic-10-vault` | M+10 Great Vault loot | DungeonSlice |

### Supported healer specs (routed to QuestionablyEpic)

Holy Paladin · Restoration Druid · Discipline Priest · Holy Priest · Restoration Shaman · Mistweaver Monk · Preservation Evoker

All other specs are routed to Raidbots.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

Python 3.10+ required. Playwright also needs its browser binary:

```bash
playwright install --with-deps chromium
```

### 2. Configure

Copy `config.example.json` → `config.json` and fill in your values:

| Field | Description |
|---|---|
| `simc_string` | Full SimulationCraft export from the `/simc` in-game addon |
| `character.region` | `eu`, `us`, `kr`, or `tw` |
| `character.realm` | Realm slug (lowercase, hyphens for spaces — e.g. `ravencrest`, `area-52`) |
| `character.name` | Character name |
| `discord_bot_token` | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `discord_channel_id` | ID of the channel where results are posted |
| `raidsid` | Your `raidsid` session cookie from raidbots.com (F12 → Application → Cookies) |
| `raidbots_api_key` | Raidbots Premium API key, or `null` for free tier |
| `runs` | Array of sim configurations (see below) |
| `timeout_minutes` | How long to wait for each sim before giving up (default: `30`) |
| `notify_on_failure` | Send a Discord message if a sim fails or times out |

#### `runs` entry fields

| Field | Description |
|---|---|
| `difficulty` | One of the difficulty keys from the table above |
| `spec` | Human-readable spec name, e.g. `"Fire"`, `"Holy"` |
| `spec_id` | WoW spec ID (e.g. `63` = Fire Mage, `65` = Holy Paladin) |
| `loot_spec_id` | Spec to loot for — usually the same as `spec_id` |
| `fight_style` | `"Patchwerk"` for raids, `"DungeonSlice"` for M+ |
| `iterations` | `"smart"` (recommended) or a fixed number |
| `crafted_stats` | Crafted gear secondary stat combo, e.g. `"36/49"` |

> **Realm slug tip:** Find yours in any raider.io URL — `https://raider.io/characters/eu/YOUR-REALM/YOUR-NAME`.

### 3. Discord bot setup

1. Create an application at [discord.com/developers](https://discord.com/developers/applications)
2. Under **Bot**, generate a token and paste it into `discord_bot_token`
3. Enable the **Message Content** and **Server Members** privileged intents
4. Invite the bot to your server with the `applications.commands` and `bot` scopes
5. Set `discord_channel_id` to the target channel ID (right-click channel → Copy Channel ID)

### 4. Run

```bash
python app.py
```

This starts the Flask web interface on `http://localhost:5000` and launches the Discord bot in a background thread (if `discord_bot_token` is set). The browser opens automatically.

---

## Docker / Coolify deployment

A `Dockerfile` and `docker-compose.yml` are included for self-hosted deployment behind Traefik (e.g. via [Coolify](https://coolify.io)).

1. Prepare config files on the host:
   ```bash
   cp config.example.json config.json   # fill in your secrets
   echo '{}' > characters.json
   echo '{}' > last_run.json
   ```
2. Set environment variables in Coolify (or a local `.env` file):
   - `DOMAIN` — your public domain, e.g. `sim.example.com`
   - `TZ` — timezone, e.g. `Europe/Berlin` (optional)
3. Deploy — Coolify picks up `docker-compose.yml` and handles TLS via Let's Encrypt.

`config.json` is mounted read-only; `characters.json` and `last_run.json` are mounted read-write so state persists across restarts.

---

## Notes & limitations

- **Raidbots has no public API.** This tool uses the same internal endpoints the website uses and may break if Raidbots changes them. Check the console or `droptimizer.log` if sims stop working.
- **QE automation uses a real browser** (headless Chromium via Playwright). It is slower than the Raidbots API path and requires `playwright install --with-deps chromium`.
- **Free-tier Raidbots queue times** can be 5–15 min during peak hours. The default `timeout_minutes: 30` covers this.
- **`raidsid` cookies expire.** If sims suddenly start failing with auth errors, grab a fresh cookie from `raidbots.com` (F12 → Application → Cookies → `raidsid`).
- **SimC string accuracy:** The `/simc` addon export is more accurate than Armory lookups. Re-export after significant gear changes.
