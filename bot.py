import os
import json
import math
import secrets
import asyncio
import logging
import platform
import traceback
import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks
from datetime import datetime, timezone

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
BOT_PORT = int(os.environ.get("BOT_PORT") or os.environ.get("HEALTH_PORT") or 2067)
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
GIT_ADDRESS = os.environ.get("GIT_ADDRESS", "")

# Shared secret used to authorise restart requests to /restart. Falls back to the
# web panel password so no extra egg variable is required.
RESTART_TOKEN = os.environ.get("RESTART_TOKEN") or os.environ.get("WEBUI_PASSWORD") or ""
RESTART_REQUIRE_TOKEN = os.environ.get("RESTART_REQUIRE_TOKEN", "1") == "1"


def get_code_revision(base_dir: str = "") -> str:
    """Best-effort short git revision of the deployed code (no git binary needed).

    Lets the web panel confirm which commit the bot is actually running.
    """
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(root, ".git", "HEAD"), "r", encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref_parts = head.split(" ", 1)[1].strip().split("/")
            with open(os.path.join(root, ".git", *ref_parts), "r", encoding="utf-8") as fh:
                head = fh.read().strip()
        return head[:7] or "unknown"
    except Exception:
        return "unknown"


CODE_REVISION = get_code_revision()

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
bot.db_pool: aiomysql.Pool | None = None
bot.start_time = datetime.now(timezone.utc)


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
# Welcome messages
# ---------------------------------------------------------------------------

DEFAULT_WELCOME_MESSAGE = "Welcome {user.mention} to **{guild.name}**!"


def apply_welcome_placeholders(template: str, member: discord.Member) -> str:
    """Substitute the supported ``{placeholders}`` in a welcome template."""
    if not template:
        return ""
    return (
        template.replace("{user.mention}", member.mention)
        .replace("{user.name}", member.name)
        .replace("{user.avatar}", member.display_avatar.url)
        .replace("{guild.name}", member.guild.name)
        .replace("{guild.icon}", member.guild.icon.url if member.guild.icon else "")
    )


def build_welcome_embed(raw_embed, member: discord.Member) -> discord.Embed | None:
    """Build a welcome embed from stored JSON and fill in the placeholders.

    Returns ``None`` when no embed is configured or it cannot be parsed so the
    caller can fall back to the plain welcome message.
    """
    if not raw_embed:
        return None
    try:
        data = json.loads(raw_embed) if isinstance(raw_embed, str) else raw_embed
        if not isinstance(data, dict):
            raise TypeError("stored welcome embed is not a JSON object")
        embed = discord.Embed.from_dict(data)
    except Exception:
        log.warning(
            "Failed to load custom welcome embed for guild %s, using default",
            member.guild.id,
        )
        return None

    if embed.title:
        embed.title = apply_welcome_placeholders(embed.title, member)
    if embed.description:
        embed.description = apply_welcome_placeholders(embed.description, member)
    footer = embed.footer
    if footer and footer.text:
        embed.set_footer(
            text=apply_welcome_placeholders(footer.text, member),
            icon_url=footer.icon_url,
        )
    if embed.thumbnail and embed.thumbnail.url:
        embed.set_thumbnail(
            url=apply_welcome_placeholders(embed.thumbnail.url, member)
        )
    if embed.image and embed.image.url:
        embed.set_image(
            url=apply_welcome_placeholders(embed.image.url, member)
        )
    return embed


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Guilds: %s", len(bot.guilds))
    if bot.owner_id is None:
        try:
            app_info = await bot.application_info()
            bot.owner_id = app_info.owner.id
        except Exception:
            log.exception("Could not determine bot owner from application info")
    if not heartbeat_loop.is_running():
        heartbeat_loop.start()
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s slash commands globally", len(synced))
        if DEV_GUILD_ID and bot.get_guild(int(DEV_GUILD_ID)):
            guild = discord.Object(id=int(DEV_GUILD_ID))
            dev_synced = await bot.tree.sync(guild=guild)
            log.info("Synced %s slash commands to dev guild", len(dev_synced))
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.event
async def on_member_join(member: discord.Member):
    settings = await get_guild_settings(member.guild.id)
    if not settings or not settings.get("welcome_channel_id"):
        return

    try:
        channel_id = int(settings["welcome_channel_id"])
    except (TypeError, ValueError):
        log.warning("Invalid welcome channel for guild %s", member.guild.id)
        return

    channel = member.guild.get_channel(channel_id)
    if not hasattr(channel, "send"):
        log.warning(
            "Welcome channel %s in guild %s is missing or not sendable",
            channel_id,
            member.guild.id,
        )
        return

    try:
        embed = build_welcome_embed(settings.get("welcome_embed"), member)
        if embed is not None:
            await channel.send(embed=embed)
            return

        msg = apply_welcome_placeholders(
            settings.get("welcome_message") or DEFAULT_WELCOME_MESSAGE, member
        )
        await channel.send(msg)
    except discord.Forbidden:
        log.warning("Missing permissions to send the welcome message in guild %s", member.guild.id)
    except discord.HTTPException:
        log.exception("Failed to send the welcome message in guild %s", member.guild.id)


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
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
# Health server (polled by webpanel.py)
# ---------------------------------------------------------------------------

def _health_payload() -> dict:
    """Build the health payload; any exception here is reported, never a 500."""
    try:
        latency_ms = round(bot.latency * 1000)
    except Exception:
        # bot.latency needs a live gateway connection (and is NaN until ready)
        latency_ms = None
    if latency_ms is not None and math.isnan(latency_ms):
        latency_ms = None
    return {
        "status": "ok",
        "ready": bot.is_ready(),
        "revision": CODE_REVISION,
        "port": BOT_PORT,
        "user": str(bot.user) if bot.user else None,
        "user_id": bot.user.id if bot.user else None,
        "guilds": len(bot.guilds),
        "guild_list": [
            {
                "id": guild.id,
                "name": guild.name,
                "member_count": guild.member_count,
                "icon": str(guild.icon.url) if guild.icon else None,
                "channels": [
                    {
                        "id": channel.id,
                        "name": channel.name,
                        "category": (
                            channel.category.name
                            if getattr(channel, "category", None)
                            else None
                        ),
                    }
                    for channel in guild.channels
                    if isinstance(channel, discord.TextChannel)
                    and not isinstance(channel, discord.CategoryChannel)
                ],
            }
            for guild in bot.guilds
        ],
        "latency_ms": latency_ms,
        "uptime_seconds": int((datetime.now(timezone.utc) - bot.start_time).total_seconds()),
        "database": bot.db_pool is not None,
        "python": platform.python_version(),
        "discordpy": discord.__version__,
    }


async def health_handler(request: web.Request) -> web.Response:
    # The panel displays the error, so always answer 200 - an HTTP 500 would
    # hide the actual exception behind a generic aiohttp error page.
    try:
        return web.json_response(_health_payload())
    except Exception as exc:
        log.exception("Failed to build the health payload")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
        return web.json_response({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": detail,
        })


async def restart_handler(request: web.Request) -> web.Response:
    if RESTART_REQUIRE_TOKEN:
        token = request.headers.get("X-Restart-Token", "")
        if not RESTART_TOKEN or not secrets.compare_digest(token.encode(), RESTART_TOKEN.encode()):
            return web.json_response(
                {"status": "forbidden", "error": "invalid or missing restart token"},
                status=403,
            )
    log.warning("Restart requested via the health endpoint")
    # Letting the process exit is what makes Pterodactyl start it again.
    asyncio.get_running_loop().call_later(0.5, os._exit, 0)
    return web.json_response({"status": "restarting"})


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/restart", restart_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BOT_PORT)
    await site.start()
    log.info("Health server listening on port %s", BOT_PORT)
    return runner


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        log.error("DISCORD_TOKEN environment variable is not set")
        return
    log.info("Solarflare bot starting (revision %s)", CODE_REVISION)

    # Neither the database nor the health port may stop the bot from running.
    try:
        await init_db()
    except Exception:
        log.exception("Database unavailable - welcome settings cannot be stored or read")
    try:
        runner = await start_health_server()
    except Exception:
        log.exception("Health server could not bind port %s - the web panel will show OFFLINE", BOT_PORT)
        runner = None

    try:
        async with bot:
            await load_cogs()
            await bot.start(BOT_TOKEN)
    finally:
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
