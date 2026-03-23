# Droptimizer Daily Bot

Automatically submits a Raidbots Droptimizer sim for your WoW character each day and posts the report link to a Discord channel.

---

## How it works

```
[cron / Task Scheduler]
        │
        ▼
droptimizer.py
        │  POST /sim  (Armory lookup)
        ▼
  Raidbots API  ──► queues & runs SimulationCraft
        │
        │  GET /api/job/{id}  (polls every 15–60s)
        ▼
   sim finishes
        │
        │  POST webhook
        ▼
  Discord channel  ◄── embed with 🔗 report link
```

---

## Setup

### 1. Install Python dependencies

```bash
pip install requests
```

Python 3.10+ recommended.

### 2. Configure

Copy `config.json.example` → `config.json` and fill in:

| Field | What to put |
|---|---|
| `character.region` | `eu`, `us`, `kr`, or `tw` |
| `character.realm` | Your realm slug (lowercase, hyphens for spaces, e.g. `ravencrest`, `area-52`) |
| `character.name` | Your character name |
| `discord_webhook_url` | See **Discord setup** below |
| `raidbots_api_key` | Your Raidbots Premium API key, or `null` for free tier |
| `droptimizer_options.instance` | Which content to sim (see config comments) |
| `droptimizer_options.keystone_level` | M+ key level (remove if simming a raid) |

> **Realm slug tip:** Go to `https://raider.io/characters/eu/YOUR-REALM/YOUR-NAME` — the URL segment is your realm slug.

### 3. Discord webhook setup

1. In your Discord server, open **Server Settings → Integrations → Webhooks**
2. Click **New Webhook**, choose a channel, copy the URL
3. Paste it into `discord_webhook_url` in your config

### 4. Test run

```bash
python droptimizer.py
```

Watch the console — it will log submission, polling progress, and the final Discord confirmation. A log file `droptimizer.log` is also written alongside the script.

---

## Scheduling

### Linux / macOS (cron)

Open your crontab:
```bash
crontab -e
```

Add a line to run at 08:00 every day:
```cron
0 8 * * * /usr/bin/python3 /path/to/droptimizer.py
```

Make the script executable first if needed:
```bash
chmod +x /path/to/droptimizer.py
```

### Windows (Task Scheduler)

1. Open **Task Scheduler → Create Basic Task**
2. Set trigger: **Daily**, choose your time
3. Action: **Start a program**
   - Program: `python`
   - Arguments: `C:\path\to\droptimizer.py`
   - Start in: `C:\path\to\` (the folder containing `config.json`)

---

## Raidbots instance slugs

Use these in `droptimizer_options.instance`:

| Slug | Content |
|---|---|
| `mythic-plus` | All current M+ dungeons |
| `liberation-of-undermine` | Liberation of Undermine (TWW S2) |
| `nerub-ar-palace` | Nerub-ar Palace (TWW S1) |
| `world-bosses` | Current world bosses |

> For raids, also set `"difficulty": "heroic"` (or `"normal"` / `"mythic"`) and remove `keystone_level`.

---

## Notes & limitations

- **Raidbots does not have a public API.** This script uses the same internal endpoints the website uses. It may break if Raidbots changes its API. Check `droptimizer.log` if sims suddenly stop working.
- **Free tier queue times** can be 5–15 min during peak hours. The script polls for up to 30 min by default (`timeout_minutes` in config).
- **Raidbots Premium** removes most queue wait and unlocks running all M+ dungeons in one sim. Your API key goes in `raidbots_api_key`.
- The Armory can be outdated if you haven't logged out of WoW recently. For maximum accuracy, swap to the `/simc` addon string method (replace `armoryRegion/Realm/Name` with a `simc` field in the payload).
