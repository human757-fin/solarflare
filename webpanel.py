import os
import json
import secrets
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template_string,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from markupsafe import Markup, escape

import pymysql
from pymysql.cursors import DictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "changeme")
BOT_TOKEN = os.environ.get("DISCORD_TOKEN", "")
BOT_PY_FILE = os.environ.get("BOT_PY_FILE", "bot.py")
WEB_PORT = int(os.environ.get("WEB_PORT", 2040))
BOT_PORT = int(os.environ.get("BOT_PORT") or os.environ.get("HEALTH_PORT") or 2067)
WEBUI_SECURE_COOKIE = os.environ.get("WEBUI_SECURE_COOKIE", "0") == "1"

# Optional explicit override; when empty the panel probes common localhost URLs.
BOT_HEALTH_URL = os.environ.get("BOT_HEALTH_URL", "")
BOT_RESTART_URL = os.environ.get("BOT_RESTART_URL", f"http://127.0.0.1:{BOT_PORT}/restart")
RESTART_TOKEN = os.environ.get("RESTART_TOKEN") or os.environ.get("WEBUI_PASSWORD") or ""

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "")
DB_SSL = os.environ.get("DB_SSL", "0") == "1"

log = logging.getLogger("webpanel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def get_code_revision(base_dir: str = "") -> str:
    """Best-effort short git revision of the deployed code (no git binary needed)."""
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


PANEL_REVISION = get_code_revision()


def get_db():
    """Synchronous MySQL connection for the panel.

    PyMySQL (installed as aiomysql's own dependency) is used here because
    driving an async MySQL client from Flask's synchronous request threads
    with ``run_until_complete`` fails in production with
    ``'_asyncio.Future' object has no attribute 'send'``.
    """
    kwargs = {"connect_timeout": 10, "charset": "utf8mb4"}
    if DB_SSL:
        kwargs["ssl"] = {"ca": None}
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, autocommit=True,
        cursorclass=DictCursor, **kwargs,
    )


_health_cache = {
    "time": 0.0, "data": None, "url": None, "error": None,
    "bot_error": None, "bot_traceback": None,
}
HEALTH_CACHE_SECONDS = 5
# Failed probes are cached longer so an offline bot does not slow every page load.
HEALTH_FAIL_CACHE_SECONDS = 15


def reset_health_cache():
    """Forget cached probe results so the next request re-checks immediately."""
    _health_cache.update(
        time=0.0, data=None, url=None, error=None,
        bot_error=None, bot_traceback=None,
    )


def health_candidates() -> list:
    """URLs to probe, in order. Explicit override first, then common localhost forms."""
    candidates = []
    if BOT_HEALTH_URL:
        candidates.append(BOT_HEALTH_URL)
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:{BOT_PORT}/health"
        if url not in candidates:
            candidates.append(url)
    return candidates


def _parse_health_body(raw) -> dict | None:
    """Parse a health endpoint body into a dict, or None when it is not JSON."""
    try:
        data = json.loads((raw or b"").decode("utf-8", "replace") or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def fetch_bot_health(timeout=2, force=False):
    """Read the bot's /health endpoint. Returns the payload, or None when offline."""
    now = time.time()
    age = now - _health_cache["time"]
    if not force:
        if _health_cache["data"] is not None and age < HEALTH_CACHE_SECONDS:
            return _health_cache["data"]
        if _health_cache["data"] is None and age < HEALTH_FAIL_CACHE_SECONDS:
            return None

    data = None
    working_url = None
    errors = []
    for candidate in health_candidates():
        try:
            with urllib.request.urlopen(candidate, timeout=timeout) as resp:
                if resp.status == 200:
                    data = _parse_health_body(resp.read())
                    if data is not None:
                        working_url = candidate
                        break
                    errors.append(f"{candidate} -> invalid JSON")
                else:
                    errors.append(f"{candidate} -> HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            # An older bot build can answer HTTP 500; surface its body so the
            # real error is visible instead of a bare status code.
            body = _parse_health_body(exc.read() or b"")
            if body and body.get("status") == "error":
                data = body
                working_url = candidate
                break
            errors.append(f"{candidate} -> HTTP {exc.code}")
        except Exception as exc:
            errors.append(f"{candidate} -> {exc}")

    bot_error = None
    bot_traceback = None
    if data:
        if data.get("status") == "error":
            bot_error = data.get("error") or "health endpoint reported an error"
            bot_traceback = data.get("traceback")
        elif data.get("error"):
            bot_error = str(data["error"])

    error = None if data else "; ".join(errors)
    if error:
        log.warning("Bot health check failed: %s", error)
    _health_cache.update(
        time=now, data=data, url=working_url, error=error,
        bot_error=bot_error, bot_traceback=bot_traceback,
    )
    return data


def check_database():
    """Return (status, error) for the panel's own database connection."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        finally:
            conn.close()
        return "connected", None
    except Exception as exc:
        return "unavailable", str(exc)


def get_db_guild_count():
    """Guilds with stored settings, or None when the database is unreachable."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS total FROM guild_settings")
                row = cursor.fetchone()
            return int(row["total"]) if row else 0
        finally:
            conn.close()
    except Exception:
        return None


def format_uptime(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def get_bot_stats():
    """Bot status for the web panel, preferring the bot's own health endpoint."""
    health = fetch_bot_health()
    if health:
        bot_error = None
        bot_traceback = None
        if health.get("status") == "error":
            bot_error = health.get("error") or "health endpoint reported an error"
            bot_traceback = health.get("traceback")
        elif health.get("error"):
            bot_error = str(health["error"])
        return {
            "status": "online",
            "guilds": int(health.get("guilds") or 0),
            "guild_list": health.get("guild_list") or [],
            "user": health.get("user"),
            "ready": bool(health.get("ready", True)),
            "latency_ms": health.get("latency_ms"),
            "uptime_seconds": health.get("uptime_seconds"),
            "database": bool(health.get("database")),
            "source": "bot",
            "revision": health.get("revision"),
            "bot_port": health.get("port"),
            "python_version": health.get("python"),
            "discordpy_version": health.get("discordpy"),
            "health_url": _health_cache.get("url"),
            "health_error": None,
            "bot_error": bot_error,
            "bot_traceback": bot_traceback,
        }

    db_guilds = get_db_guild_count()
    return {
        "status": "offline",
        "guilds": db_guilds or 0,
        "guild_list": [],
        "user": None,
        "ready": False,
        "latency_ms": None,
        "uptime_seconds": None,
        "database": db_guilds is not None,
        "source": "db",
        "revision": None,
        "bot_port": None,
        "python_version": None,
        "discordpy_version": None,
        "health_url": None,
        "health_error": _health_cache.get("error"),
        "bot_error": None,
        "bot_traceback": None,
    }


DEFAULT_EMBED_COLOR = 0x5865F2


def parse_hex_color(value: str, default: int = DEFAULT_EMBED_COLOR) -> int:
    """Parse a ``#RRGGBB`` / ``RRGGBB`` string into an int, falling back to default."""
    try:
        color = int(str(value).replace("#", "").strip(), 16)
    except (TypeError, ValueError):
        return default
    if not 0 <= color <= 0xFFFFFF:
        return default
    return color


def color_to_hex(value) -> str:
    """Render a stored embed color as a ``RRGGBB`` string for form inputs."""
    try:
        return f"{int(value):06x}"
    except (TypeError, ValueError):
        return f"{DEFAULT_EMBED_COLOR:06x}"


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# HTML Templates (inline for single-file deployment)
# ---------------------------------------------------------------------------

BASE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f0f13; color: #dcddde; min-height: 100vh; }
a { color: #5865f2; text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 900px; margin: 0 auto; padding: 2rem; }
h1 { font-size: 1.8rem; margin-bottom: 1rem; color: #fff; }
h2 { font-size: 1.3rem; margin: 1.5rem 0 0.75rem; color: #b9bbbe; }
.card { background: #18191c; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; border: 1px solid #2f3136; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
.stat-box { background: #202225; border-radius: 8px; padding: 1.2rem; text-align: center; border: 1px solid #2f3136; }
.stat-box .label { font-size: 0.8rem; color: #72767d; text-transform: uppercase; letter-spacing: 0.05em; }
.stat-box .value { font-size: 2rem; font-weight: 700; color: #fff; margin-top: 0.25rem; }
.status-online { color: #43b581; }
.status-offline { color: #f04747; }
.status-unknown { color: #faa61a; }
.nav { display: flex; gap: 1rem; margin-bottom: 2rem; padding: 1rem; background: #18191c; border-radius: 8px; border: 1px solid #2f3136; }
.nav a { padding: 0.5rem 1rem; border-radius: 6px; color: #b9bbbe; }
.nav a:hover, .nav a.active { background: #5865f2; color: #fff; text-decoration: none; }
input, textarea, select { width: 100%; padding: 0.75rem; background: #202225; border: 1px solid #2f3136; border-radius: 6px; color: #dcddde; font-size: 0.95rem; margin-bottom: 0.75rem; }
input:focus, textarea:focus { outline: none; border-color: #5865f2; }
textarea { min-height: 120px; resize: vertical; font-family: monospace; }
label { display: block; margin-bottom: 0.3rem; color: #b9bbbe; font-size: 0.85rem; }
.btn { display: inline-block; padding: 0.6rem 1.5rem; border: none; border-radius: 6px; cursor: pointer; font-size: 0.95rem; font-weight: 600; }
.btn-primary { background: #5865f2; color: #fff; }
.btn-primary:hover { background: #4752c4; }
.btn-danger { background: #f04747; color: #fff; }
.btn-danger:hover { background: #d83c3c; }
.btn-success { background: #43b581; color: #fff; }
.btn-success:hover { background: #3ca374; }
.flash { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
.flash-error { background: #f0474722; border: 1px solid #f04747; color: #f04747; }
.flash-success { background: #43b58122; border: 1px solid #43b581; color: #43b581; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid #2f3136; }
th { color: #72767d; font-size: 0.8rem; text-transform: uppercase; }
.guild-row:hover { background: #202225; }
.preview-box { background: #2f3136; border-radius: 8px; padding: 1rem; margin-top: 0.5rem; }
.preview-embed { border-left: 4px solid #5865f2; padding: 0.75rem 1rem; background: #2f3136; border-radius: 0 4px 4px 0; margin-top: 0.5rem; }
.login-wrapper { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.login-box { background: #18191c; padding: 2.5rem; border-radius: 12px; border: 1px solid #2f3136; width: 100%; max-width: 380px; }
.login-box h1 { text-align: center; }
"""

LAYOUT = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} — Solarflare</title><style>{{ css }}</style></head>
<body>
<div class="container">
<div class="nav">
  <a href="/" class="{{ 'active' if page=='status' }}">Status</a>
  <a href="/dashboard" class="{{ 'active' if page=='dashboard' }}">Dashboard</a>
  <a href="/welcome-editor" class="{{ 'active' if page=='welcome' }}">Welcome Editor</a>
  <a href="/diagnostics" class="{{ 'active' if page=='diagnostics' }}">Diagnostics</a>
  <a href="/logout" style="margin-left:auto">Logout</a>
</div>
{% with messages = get_flashed_messages(with_categories=true) %}
{% for cat, msg in messages %}
<div class="flash flash-{{ cat }}">{{ msg }}</div>
{% endfor %}{% endwith %}
{{ body }}
</div></body></html>"""


def render_page(title, body, page=""):
    return render_template_string(
        LAYOUT,
        title=title,
        body=Markup(body),
        css=Markup(BASE_CSS),
        page=page,
    )


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.route("/")
def status():
    if request.args.get("recheck"):
        reset_health_cache()
    stats = get_bot_stats()
    status_class = f"status-{stats['status']}"
    guild_label = "Guilds" if stats["status"] == "online" else "Configured Guilds"

    if stats["status"] == "online":
        bot_error_note = (
            f'<p style="color:#faa61a">Bot error: <code>{escape(str(stats.get("bot_error")))}</code> - '
            '<a href="/diagnostics?recheck=1">see Diagnostics for the traceback</a>.</p>'
            if stats.get("bot_error") else ""
        )
        health_details = f"""
      <p>Bot user: <code>{escape(stats['user'] or 'unknown')}</code></p>
      <p>Gateway: <code>{'ready' if stats['ready'] else 'connecting'}</code></p>
      <p>Latency: <code>{stats['latency_ms'] if stats['latency_ms'] is not None else 'n/a'} ms</code></p>
      <p>Uptime: <code>{format_uptime(stats['uptime_seconds'])}</code></p>
      <p>Database (bot): <code>{'connected' if stats['database'] else 'unavailable'}</code></p>
      <p>Bot revision: <code>{escape(str(stats['revision'] or 'not reported'))}</code></p>
      <p>Answered on: <code>{escape(str(stats['health_url'] or '-'))}</code></p>
      {bot_error_note}
        """
    else:
        health_details = f"""
      <p>The bot did not answer its health endpoint, so the numbers above fall back
      to how many guild settings are stored.</p>
      <p>Probed: <code>{escape(', '.join(health_candidates()))}</code></p>
      <p>Last error: <code>{escape(str(stats['health_error'] or 'unknown'))}</code></p>
      <p><a href="/diagnostics?recheck=1">Open Diagnostics</a> (re-checks the bot now).</p>
        """

    body = f"""
    <h1>Solarflare Bot</h1>
    <div class="stat-grid">
      <div class="stat-box"><div class="label">Status</div><div class="value {status_class}">{stats['status'].upper()}</div></div>
      <div class="stat-box"><div class="label">{guild_label}</div><div class="value">{stats['guilds']}</div></div>
    </div>
    <div class="card" style="margin-top:1rem">
      <h2>Health Check</h2>
      {health_details}
    </div>
    <div class="card">
      <h2>System</h2>
      <p>Bot process: <code>{BOT_PY_FILE}</code></p>
      <p>Uptime clock resets on restart. Check Pterodactyl panel for exact uptime.</p>
    </div>
    """
    return render_page("Status", body, "status")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password.encode(), WEBUI_PASSWORD.encode()):
            session["authenticated"] = True
            session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=12)
            app.session_cookie_secure = WEBUI_SECURE_COOKIE
            return redirect(url_for("dashboard"))
        flash("Invalid password", "error")
    body = """
    <div class="login-wrapper"><div class="login-box">
      <h1>Login</h1>
      <form method="post">
        <label>Password</label>
        <input type="password" name="password" autofocus>
        <button class="btn btn-primary" style="width:100%;margin-top:0.5rem">Enter</button>
      </form>
    </div></div>
    """
    return render_page("Login", body, "login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("status"))


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    stats = get_bot_stats()
    settings_by_guild = {}
    try:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM guild_settings ORDER BY guild_id")
                for row in cursor.fetchall():
                    settings_by_guild[int(row["guild_id"])] = row
        finally:
            conn.close()
    except Exception as e:
        flash(f"DB error: {e}", "error")

    # Guilds the bot is actually in, plus any leftover rows for guilds it has left.
    known_guilds = {}
    for guild in stats["guild_list"]:
        known_guilds[int(guild["id"])] = {
            "name": guild.get("name") or "Unknown",
            "members": guild.get("member_count"),
        }
    for guild_id in settings_by_guild:
        known_guilds.setdefault(guild_id, {"name": "(not joined anymore)", "members": None})

    rows = ""
    for guild_id, info in known_guilds.items():
        settings = settings_by_guild.get(guild_id, {})
        wc = settings.get("welcome_channel_id") or "—"
        embed_flag = "Yes" if settings.get("welcome_embed") else "No"
        members = info["members"] if info["members"] is not None else "—"
        rows += f"""<tr class="guild-row">
          <td>{escape(info['name'])}<br><span style="color:#72767d">{guild_id}</span></td>
          <td>{members}</td><td>{wc}</td><td>{embed_flag}</td>
          <td><a href="/welcome-editor?guild_id={guild_id}">Edit</a></td>
        </tr>"""
    if not rows:
        rows = ('<tr><td colspan="5" style="color:#72767d">'
                'No guilds detected — is the bot running and connected to Discord?</td></tr>')

    body = f"""
    <h1>Dashboard</h1>
    <div class="card">
      <h2>Controls</h2>
      <div style="display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:0.5rem">
        <form method="post" action="/api/restart"><button class="btn btn-danger">Restart Bot</button></form>
      </div>
    </div>
    <div class="card">
      <h2>Guilds</h2>
      <table>
        <tr><th>Guild</th><th>Members</th><th>Welcome Channel</th><th>Custom Embed</th><th></th></tr>
        {rows}
      </table>
    </div>
    """
    return render_page("Dashboard", body, "dashboard")


@app.route("/welcome-editor", methods=["GET", "POST"])
@login_required
def welcome_editor():
    guild_id = (request.args.get("guild_id") or request.form.get("guild_id", "")).strip()
    if guild_id and not guild_id.isdigit():
        flash("Guild ID must be a number", "error")
        guild_id = ""

    settings = None
    if guild_id:
        try:
            conn = get_db()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM guild_settings WHERE guild_id = %s", (int(guild_id),)
                    )
                    settings = cursor.fetchone()
            finally:
                conn.close()
        except Exception as e:
            flash(f"DB error: {e}", "error")

    # Only the "Save Settings" form posts; the guild picker uses GET, so loading
    # a guild's settings can never overwrite them.
    if request.method == "POST" and guild_id:
        welcome_channel = request.form.get("welcome_channel_id", "").strip()
        welcome_message = request.form.get("welcome_message", "").strip()
        embed_title = request.form.get("embed_title", "").strip()
        embed_description = request.form.get("embed_description", "").strip()
        embed_color = request.form.get("embed_color", "").strip()
        embed_thumbnail = request.form.get("embed_thumbnail", "").strip()
        embed_image = request.form.get("embed_image", "").strip()
        embed_footer = request.form.get("embed_footer", "").strip()

        if welcome_channel and not welcome_channel.isdigit():
            flash("Welcome channel ID must be a number", "error")
            return redirect(f"/welcome-editor?guild_id={guild_id}")

        embed_data = None
        if embed_title or embed_description or embed_thumbnail or embed_image or embed_footer:
            embed_data = {
                "title": embed_title,
                "description": embed_description,
                "color": parse_hex_color(embed_color),
            }
            if embed_thumbnail:
                embed_data["thumbnail"] = {"url": embed_thumbnail}
            if embed_image:
                embed_data["image"] = {"url": embed_image}
            if embed_footer:
                embed_data["footer"] = {"text": embed_footer}

        try:
            conn = get_db()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO guild_settings (guild_id, welcome_channel_id, welcome_message, welcome_embed)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            welcome_channel_id = VALUES(welcome_channel_id),
                            welcome_message = VALUES(welcome_message),
                            welcome_embed = VALUES(welcome_embed)
                        """,
                        (
                            int(guild_id),
                            int(welcome_channel) if welcome_channel else None,
                            welcome_message or None,
                            json.dumps(embed_data) if embed_data else None,
                        ),
                    )
            finally:
                conn.close()
            flash("Welcome settings saved", "success")
        except Exception as e:
            flash(f"DB error: {e}", "error")

        return redirect(f"/welcome-editor?guild_id={guild_id}")

    known_links = ""
    for guild in get_bot_stats()["guild_list"]:
        known_links += (
            f'<li><a href="/welcome-editor?guild_id={int(guild["id"])}">'
            f'{escape(guild.get("name") or "Unknown")} '
            f'<span style="color:#72767d">{int(guild["id"])}</span></a></li>'
        )
    known_block = (
        f'<ul style="margin-top:0.5rem;list-style:none;padding:0">{known_links}</ul>'
        if known_links
        else '<p style="color:#72767d;margin-top:0.5rem">No guilds reported by the bot — '
             'start the bot or enter an ID manually.</p>'
    )

    wc = (settings.get("welcome_channel_id") or "") if settings else ""
    wm = (settings.get("welcome_message") or "") if settings else ""
    embed = {}
    if settings and settings.get("welcome_embed"):
        raw_embed = settings["welcome_embed"]
        try:
            embed = raw_embed if isinstance(raw_embed, dict) else json.loads(raw_embed)
        except Exception:
            embed = {}
    if not isinstance(embed, dict):
        embed = {}

    thumbnail = embed.get("thumbnail")
    thumbnail_url = thumbnail.get("url", "") if isinstance(thumbnail, dict) else ""
    image = embed.get("image")
    image_url = image.get("url", "") if isinstance(image, dict) else ""
    footer = embed.get("footer")
    footer_text = footer.get("text", "") if isinstance(footer, dict) else ""

    body = f"""
    <h1>Welcome Embed Editor</h1>
    <form method="get" action="/welcome-editor">
      <div class="card">
        <h2>Select Guild</h2>
        <label>Guild ID</label>
        <input type="number" name="guild_id" value="{escape(guild_id)}" placeholder="Discord Guild ID" required>
        <button class="btn btn-primary" type="submit" style="margin-top:0.25rem">Load</button>
        <h2>Guilds The Bot Is In</h2>
        {known_block}
      </div>
    </form>
    """

    if guild_id:
        guild_info = next(
            (
                g for g in get_bot_stats()["guild_list"]
                if str(g.get("id")) == str(guild_id)
            ),
            None,
        )
        channels = (guild_info or {}).get("channels") or []
        by_category = {}
        for ch in channels:
            by_category.setdefault(ch.get("category") or "General", []).append(ch)
        options = '<option value="">— no channel —</option>'
        for category in sorted(by_category, key=str.lower):
            options += f'<optgroup label="{escape(str(category))}">'
            for ch in sorted(by_category[category], key=lambda c: str(c.get("name", "")).lower()):
                selected = ' selected' if str(ch.get("id")) == str(wc) else ''
                options += (
                    f'<option value="{int(ch["id"])}"{selected}>'
                    f'{escape(str(ch.get("name") or "unknown"))}'
                    f'</option>'
                )
            options += "</optgroup>"
        if not channels:
            options += (
                '<option value="" disabled>No text channels reported — '
                'is the bot connected to this guild?</option>'
            )

        body += f"""
      <form method="post" action="/welcome-editor">
      <input type="hidden" name="guild_id" value="{escape(guild_id)}">
      <div class="card">
        <h2>Channel</h2>
        <label>Welcome Channel</label>
        <select name="welcome_channel_id">{options}</select>
        <label>Default Welcome Message (used if no embed)</label>
        <input type="text" name="welcome_message" value="{escape(wm)}" placeholder="Welcome {{user.mention}} to {{guild.name}}!">
      </div>
      <div class="card">
        <h2>Embed Settings</h2>
        <label>Title</label>
        <input type="text" name="embed_title" value="{escape(embed.get('title') or '')}" placeholder="Welcome!">
        <label>Description</label>
        <textarea name="embed_description" placeholder="Use {{user.mention}}, {{user.name}}, {{user.avatar}}, {{guild.name}}, {{guild.icon}}">{escape(embed.get('description') or '')}</textarea>
        <label>Color (hex)</label>
        <input type="text" name="embed_color" value="#{color_to_hex(embed.get('color', DEFAULT_EMBED_COLOR))}" placeholder="5865F2">
        <label>Thumbnail URL</label>
        <input type="text" name="embed_thumbnail" value="{escape(thumbnail_url)}" placeholder="https://... or {{user.avatar}}">
        <label>Image URL <span style="color:#72767d">(large banner image)</span></label>
        <input type="text" name="embed_image" value="{escape(image_url)}" placeholder="https://... or {{user.avatar}}">
        <label>Footer Text</label>
        <input type="text" name="embed_footer" value="{escape(footer_text)}" placeholder="Thanks for joining!">
        <button class="btn btn-success" type="submit" style="margin-top:0.5rem">Save Settings</button>
      </div>
      </form>
      """
    return render_page("Welcome Editor", body, "welcome")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@app.route("/diagnostics")
@login_required
def diagnostics():
    if request.args.get("recheck"):
        reset_health_cache()
    stats = get_bot_stats()
    panel_db_status, panel_db_error = check_database()

    def env_row(name):
        value = os.environ.get(name, "")
        return (
            f"<tr><td><code>{escape(name)}</code></td>"
            f"<td><code>{escape(value) if value else 'not set'}</code></td></tr>"
        )

    guild_rows = ""
    for guild in stats["guild_list"]:
        guild_rows += (
            f"<tr><td>{escape(guild.get('name') or 'Unknown')}</td>"
            f"<td>{int(guild['id'])}</td><td>{guild.get('member_count', '-')}</td></tr>"
        )
    if not guild_rows:
        guild_rows = '<tr><td colspan="3" style="color:#72767d">No guild data reported by the bot</td></tr>'

    online = stats["status"] == "online"
    health_colour = "#3ba55c" if online else "#ed4245"
    port_match = (
        "-" if not online
        else ("yes" if str(stats["bot_port"]) == str(BOT_PORT) else "NO - panel and bot disagree")
    )
    bot_error = stats.get("bot_error")
    bot_traceback = stats.get("bot_traceback")
    bot_error_note = (
        '<p style="color:#faa61a">The bot answered its health endpoint but reported an '
        'internal error - see the Bot Error card below.</p>'
        if bot_error else ""
    )
    bot_error_block = ""
    if bot_error:
        tb_html = (
            '<pre style="background:#16161a;border:1px solid #ed4245;border-radius:6px;'
            'padding:0.75rem;overflow-x:auto;white-space:pre-wrap;word-break:break-word">'
            f'{escape(str(bot_traceback))}</pre>'
            if bot_traceback else ""
        )
        bot_error_block = (
            '<div class="card" style="border:1px solid #ed4245">'
            '<h2 style="color:#ed4245">Bot Error</h2>'
            '<p>The bot process is running and answered its health endpoint, but the endpoint '
            'reported an internal error:</p>'
            f'<p><code>{escape(str(bot_error))}</code></p>{tb_html}'
            '<p style="color:#faa61a;margin-top:0.5rem">This error is why the panel cannot show '
            'the bot&#39;s real status. If the bot revision below is older than the panel revision, '
            'restart the server from Pterodactyl so it pulls the fixed code.</p>'
            '</div>'
        )
    restart_hint = (
        '<form method="post" action="/api/restart"><button class="btn btn-danger">Restart Bot</button></form>'
        if online else
        '<p style="color:#faa61a">The bot process is not answering, so the restart endpoint '
        'cannot be reached from here. Restart it from the Pterodactyl panel, then reload this page.</p>'
    )

    body = f"""
    <h1>Diagnostics</h1>
    <div class="card">
      <h2>Bot Health Endpoint</h2>
      <p style="font-size:1.2rem">Result: <strong style="color:{health_colour}">{'REACHABLE' if online else 'UNREACHABLE'}</strong></p>
      <p>Probed URLs: <code>{escape(', '.join(health_candidates()))}</code></p>
      <p>Answered on: <code>{escape(str(stats['health_url'] or '-'))}</code></p>
      <p>Last error: <code>{escape(str(stats['health_error'] or '-'))}</code></p>
      {bot_error_note}
      <p><a href="/diagnostics?recheck=1">Recheck now</a></p>
      <div style="margin-top:0.5rem">{restart_hint}</div>
    </div>
    {bot_error_block}
    <div class="card">
      <h2>Versions And Ports</h2>
      <table>
        <tr><th>Item</th><th>Value</th></tr>
        <tr><td>Panel revision</td><td><code>{escape(PANEL_REVISION)}</code></td></tr>
        <tr><td>Bot revision</td><td><code>{escape(str(stats['revision'] or 'not reported'))}</code></td></tr>
        <tr><td>Panel BOT_PORT</td><td><code>{BOT_PORT}</code></td></tr>
        <tr><td>Bot BOT_PORT</td><td><code>{escape(str(stats['bot_port'] if stats['bot_port'] is not None else '-'))}</code></td></tr>
        <tr><td>Ports match</td><td><code>{port_match}</code></td></tr>
        <tr><td>Restart URL</td><td><code>{escape(BOT_RESTART_URL)}</code></td></tr>
        <tr><td>Panel port</td><td><code>{WEB_PORT}</code></td></tr>
      </table>
      <p style="color:#72767d;margin-top:0.5rem">If the bot revision is older than the panel revision,
      the server has not pulled the latest code yet - restart it from Pterodactyl.</p>
    </div>
    <div class="card">
      <h2>Databases</h2>
      <table>
        <tr><th>Check</th><th>Value</th></tr>
        <tr><td>Panel connection</td><td><code>{escape(panel_db_status)}</code></td></tr>
        <tr><td>Panel error</td><td><code>{escape(panel_db_error or '-')}</code></td></tr>
        <tr><td>Bot connection</td><td><code>{'connected' if stats['database'] else ('unknown' if stats['health_error'] else 'unavailable')}</code></td></tr>
      </table>
    </div>
    <div class="card">
      <h2>Guilds Reported By The Bot ({stats['guilds']})</h2>
      <table>
        <tr><th>Name</th><th>ID</th><th>Members</th></tr>
        {guild_rows}
      </table>
      <p style="color:#72767d;margin-top:0.5rem">A count of 0 while the endpoint is reachable means the
      bot is connected but Discord has not sent its guild list yet (or the Members/Guilds intent is off).</p>
    </div>
    <div class="card">
      <h2>Relevant Environment</h2>
      <table>
        <tr><th>Variable</th><th>Value</th></tr>
        {env_row('BOT_PORT')}{env_row('HEALTH_PORT')}{env_row('BOT_HEALTH_URL')}{env_row('BOT_RESTART_URL')}
        {env_row('BOT_PY_FILE')}{env_row('WEB_PORT')}{env_row('DATABASE_ENGINE')}{env_row('DB_HOST')}
        {env_row('DB_PORT')}{env_row('DB_NAME')}{env_row('DB_USER')}{env_row('DB_SSL')}{env_row('LOG_LEVEL')}
        {env_row('RESTART_REQUIRE_TOKEN')}
        <tr><td><code>RESTART_TOKEN</code></td><td><code>{'set' if RESTART_TOKEN else 'not set'}</code></td></tr>
      </table>
      <p style="color:#72767d;margin-top:0.5rem">Secrets (tokens and passwords) are never shown here.</p>
    </div>
    """
    return render_page("Diagnostics", body, "diagnostics")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/restart", methods=["POST"])
@login_required
def api_restart():
    try:
        req = urllib.request.Request(
            BOT_RESTART_URL,
            method="POST",
            headers={"X-Restart-Token": RESTART_TOKEN},
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        _health_cache.update(time=0.0, data=None, url=None, error=None)
        flash("Bot restart signal sent", "success")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            flash("Restart refused: the panel and bot do not share a restart token", "error")
        else:
            flash(f"Restart failed: HTTP {e.code}", "error")
    except Exception as e:
        flash(f"Restart failed: {e}", "error")
    return redirect(url_for("dashboard"))


@app.route("/api/status")
def api_status():
    if request.args.get("recheck"):
        reset_health_cache()
    stats = get_bot_stats()
    return stats


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
