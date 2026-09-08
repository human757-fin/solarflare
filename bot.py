import os
import json
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands, tasks
from datetime import datetime

import aiomysql

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format=LOG_FORMAT)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_NAME = os.environ.get("DB_NAME")
DB_SSL = os.environ.get("DB_SSL", "0") == "1"
DATABASE_ENGINE = os.environ.get("DATABASE_ENGINE", "mysql")
BOT_PORT = int(os.environ.get("BOT_PORT", 2067))
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
GIT_ADDRESS = os.environ.get("GIT_ADDRESS", "")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
bot.db_pool: aiomysql.Pool | None = None
bot.start_time = datetime.utcnow()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db():
    kwargs = {}
    if DB_SSL:
        kwargs["ssl"] = {"ca": None}
    bot.db_pool = await aiomysql.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        autocommit=True,
        minsize=1,
        maxsize=10,
        **kwargs,
    )
    async with bot.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
                    welcome_channel_id BIGINT UNSIGNED DEFAULT NULL,
                    welcome_message TEXT DEFAULT NULL,
                    welcome_embed JSON DEFAULT NULL,
                    log_channel_id BIGINT UNSIGNED DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)
    log.info("Database initialised")


async def get_guild_settings(guild_id: int) -> dict | None:
    if not bot.db_pool:
        return None
    async with bot.db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM guild_settings WHERE guild_id = %s", (guild_id,)
            )
            return await cur.fetchone()


async def upsert_guild_settings(guild_id: int, **kwargs):
    if not bot.db_pool:
        return
    columns = ["guild_id"] + list(kwargs.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{k} = VALUES({k})" for k in kwargs)
    values = [guild_id] + list(kwargs.values())
    async with bot.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO guild_settings ({', '.join(columns)})
                VALUES ({placeholders})
                ON DUPLICATE KEY UPDATE {updates}
                """,
                values,
            )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Guilds: %s", len(bot.guilds))
    if not heartbeat_loop.is_running():
        heartbeat_loop.start()
    try:
        if DEV_GUILD_ID and bot.get_guild(int(DEV_GUILD_ID)):
            guild = discord.Object(id=int(DEV_GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %s slash commands to dev guild", len(synced))
        else:
            synced = await bot.tree.sync()
            log.info("Synced %s slash commands", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.event
async def on_member_join(member: discord.Member):
    settings = await get_guild_settings(member.guild.id)
    if not settings or not settings.get("welcome_channel_id"):
        return

    channel = member.guild.get_channel(settings["welcome_channel_id"])
    if not channel:
        return

    raw_embed = settings.get("welcome_embed")
    if raw_embed:
        try:
            if isinstance(raw_embed, str):
                data = json.loads(raw_embed)
            else:
                data = raw_embed
            embed = discord.Embed.from_dict(data)
            embed.description = (embed.description or "").replace(
                "{user.mention}", member.mention
            ).replace("{user.name}", member.name).replace(
                "{guild.name}", member.guild.name
            )
            await channel.send(member.mention, embed=embed)
            return
        except Exception:
            log.warning("Failed to load custom embed for guild %s, using default", member.guild.id)

    msg = (settings.get("welcome_message") or "Welcome {user.mention} to **{guild.name}**!")
    msg = msg.replace("{user.mention}", member.mention).replace(
        "{user.name}", member.name
    ).replace("{guild.name}", member.guild.name)
    await channel.send(msg)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

@tasks.loop(minutes=5)
async def heartbeat_loop():
    if WEBHOOK_URL:
        async with aiohttp.ClientSession() as session:
            payload = {
                "embeds": [{
                    "title": "Bot Heartbeat",
                    "description": f"Online — {len(bot.guilds)} guilds",
                    "color": 0x00FF00,
                    "timestamp": datetime.utcnow().isoformat(),
                }]
            }
            try:
                await session.post(WEBHOOK_URL, json=payload, timeout=10)
            except Exception:
                log.warning("Heartbeat webhook failed")


# ---------------------------------------------------------------------------
# Cog loader
# ---------------------------------------------------------------------------

async def load_cogs():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("_"):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                log.info("Loaded cog: %s", filename)
            except Exception:
                log.exception("Failed to load cog: %s", filename)


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="ping", description="Bot latency")
async def slash_ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong — {latency_ms}ms")


@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context):
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"Pong — {latency_ms}ms")


@bot.tree.command(name="serverinfo", description="Server information")
async def slash_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=0x5865F2)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention if g.owner else "N/A")
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="embed", description="Send an embed to a channel")
@discord.app_commands.describe(
    channel="Target channel",
    title="Embed title",
    description="Embed description",
    color="Hex color (e.g. ff0000)",
)
async def slash_embed(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    color: str = "5865F2",
):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message(
            "You need **Manage Messages** permission.", ephemeral=True
        )
    try:
        c = int(color.replace("#", ""), 16)
    except ValueError:
        c = 0x5865F2
    embed = discord.Embed(title=title, description=description, color=c)
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Embed sent to {channel.mention}", ephemeral=True)


@bot.tree.command(name="reload", description="Reload a cog (owner only)")
@discord.app_commands.describe(cog="Cog name")
async def slash_reload(interaction: discord.Interaction, cog: str):
    if interaction.user.id != bot.owner_id:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    try:
        await bot.reload_extension(f"cogs.{cog}")
        await interaction.response.send_message(f"Reloaded `{cog}`", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

def run_health_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
            else:
                body = b'{"status":"unknown"}'
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/restart":
                body = b'{"status":"restarting"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                threading.Thread(target=os._exit, args=(0,), daemon=True).start()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", BOT_PORT), HealthHandler)
    log.info("Health server listening on port %s", BOT_PORT)
    server.serve_forever()


import threading
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        log.error("DISCORD_TOKEN environment variable is not set")
        return
    await init_db()
    async with bot:
        await load_cogs()
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
