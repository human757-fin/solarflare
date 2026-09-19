# Solarflare

Discord bot + web panel for Pterodactyl.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Discord bot (slash commands, welcome events, embed sender, cog loader) |
| `webpanel.py` | Flask web panel (public status page + login-protected dashboard + welcome embed editor) |
| `requirements.txt` | Python dependencies |
| `startup.sh` | Pterodactyl startup script (auto-pulls, installs deps, runs panel + bot) |

## Environment Variables

All set in the Pterodactyl panel under **Startup → Variables**.

### Bot

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `BOT_PY_FILE` | Bot filename (default `bot.py`) |
| `BOT_PORT` | Bot health service port (default `2067`) |
| `DEV_GUILD_ID` | Optional server ID for immediate command syncing |
| `LOG_LEVEL` | Console logging detail (default `INFO`) |

### Database (MySQL)

| Variable | Description |
|---|---|
| `DATABASE_ENGINE` | Database type (default `mysql`) |
| `DB_HOST` | MySQL host (default `127.0.0.1`) |
| `DB_PORT` | MySQL port (default `3306`) |
| `DB_USER` | MySQL username |
| `DB_PASSWORD` | MySQL password |
| `DB_NAME` | MySQL database name |
| `DB_SSL` | Enable TLS for DB connection (`1`/`0`) |

### Web Panel

| Variable | Description |
|---|---|
| `WEBUI_PASSWORD` | Login password for the control panel |
| `WEB_PORT` | Panel port (default `2040`) |
| `WEBUI_SECURE_COOKIE` | Require HTTPS for login cookie (`1`/`0`) |

### Optional

| Variable | Description |
|---|---|
| `WEBHOOK_URL` | Discord webhook for heartbeat notifications |
| `BOT_HEALTH_URL` | Health endpoint the panel polls (default `http://127.0.0.1:$BOT_PORT/health`) |
| `BOT_RESTART_URL` | Restart endpoint used by the Dashboard button (default `http://127.0.0.1:$BOT_PORT/restart`) |
| `GIT_ADDRESS` | Git repository URL (used by egg) |
| `BRANCH` | Git branch to pull (default `main`) |

## GitHub Secrets (for auto-deploy workflow)

| Secret | Description |
|---|---|
| `PTERODACTYL_URL` | Panel URL (e.g. `https://panel.example.com`) |
| `PTERODACTYL_SERVER_ID` | Server ID from the panel URL |
| `PTERODACTYL_API_KEY` | Client API key with server access |

Push to `main` or `master` triggers a server restart via the Pterodactyl Client API.

## Web Panel

- **`/`** — Public status page (bot online/offline, guild count, latency, uptime)
- **`/login`** — Password login (uses `WEBUI_PASSWORD`)
- **`/dashboard`** — Control panel (restart button, every guild the bot is in)
- **`/welcome-editor`** — Edit welcome embeds per guild (title, description, color, thumbnail, footer)
- **`/api/status`** — JSON status endpoint
- **Bot `/health`** on `BOT_PORT` — JSON status consumed by the panel (also `/restart`)

### How the panel detects the bot

The panel polls the bot's `/health` endpoint (`BOT_HEALTH_URL`, default
`http://127.0.0.1:$BOT_PORT/health`). It returns the bot user, gateway readiness,
latency, uptime, database state and the full `guild_list` (id, name, member count)
so the Dashboard and Welcome Editor show the guilds the bot is really in.

If `/health` does not answer, the panel shows **OFFLINE** and falls back to counting
rows in `guild_settings`. Set `BOT_HEALTH_URL` / `BOT_RESTART_URL` if the panel runs
on a different host than the bot.

## Bot Commands

| Command | Type | Description |
|---|---|---|
| `/ping` | Slash | Bot latency |
| `/serverinfo` | Slash | Server info |
| `/embed` | Slash | Send a custom embed to a channel |
| `/reload` | Slash | Reload a cog (owner only) |
| `!ping` | Prefix | Bot latency |

## Welcome System

Placeholders: `{user.mention}`, `{user.name}`, `{guild.name}` — they are replaced
in the welcome message and in the embed title, description and footer.

- If a guild has a `welcome_embed` set, it sends that embed
- Otherwise falls back to `welcome_message` text
- Configure via `/welcome-editor` in the web panel: pick a guild with **Load**,
  then save with **Save Settings**

## Cogs

Drop `.py` files into `./cogs/` — they auto-load on startup.

```bash
# create example cog
mkdir -p cogs
cat > cogs/example.py << 'EOF'
import discord
from discord.ext import commands

class Example(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="hello", description="Say hello")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("Hello!")

async def setup(bot):
    await bot.add_cog(Example(bot))
EOF
```
