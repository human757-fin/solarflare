import os
import json
import secrets
import logging
import subprocess
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

from markupsafe import Markup

import aiomysql
import asyncio

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "changeme")
BOT_TOKEN = os.environ.get("DISCORD_TOKEN", "")
BOT_PY_FILE = os.environ.get("BOT_PY_FILE", "bot.py")
WEB_PORT = int(os.environ.get("WEB_PORT", 2040))
BOT_PORT = int(os.environ.get("BOT_PORT", 2067))
WEBUI_SECURE_COOKIE = os.environ.get("WEBUI_SECURE_COOKIE", "0") == "1"

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "")
DB_SSL = os.environ.get("DB_SSL", "0") == "1"

log = logging.getLogger("webpanel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def get_db():
    loop = asyncio.new_event_loop()
    kwargs = {}
    if DB_SSL:
        kwargs["ssl"] = {"ca": None}
    conn = loop.run_until_complete(
        aiomysql.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, db=DB_NAME, autocommit=True,
            **kwargs,
        )
    )
    return conn, loop


def get_bot_stats():
    stats = {"guilds": 0, "status": "unknown"}
    try:
        conn, loop = get_db()
        cursor = loop.run_until_complete(conn.cursor(aiomysql.DictCursor))
        loop.run_until_complete(
            cursor.execute("SELECT * FROM guild_settings LIMIT 100")
        )
        rows = loop.run_until_complete(cursor.fetchall())
        conn.close()
        loop.close()
        stats["guilds"] = len(rows)
    except Exception:
        pass
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
                f"http://127.0.0.1:{BOT_PORT}/health",
            ],
            capture_output=True, timeout=5,
        )
        stats["status"] = "online" if result.stdout.decode().startswith("2") else "offline"
    except Exception:
        stats["status"] = "offline"
    return stats


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
    stats = get_bot_stats()
    status_class = f"status-{stats['status']}"
    body = f"""
    <h1>Solarflare Bot</h1>
    <div class="stat-grid">
      <div class="stat-box"><div class="label">Status</div><div class="value {status_class}">{stats['status'].upper()}</div></div>
      <div class="stat-box"><div class="label">Guilds</div><div class="value">{stats['guilds']}</div></div>
    </div>
    <div class="card" style="margin-top:1rem">
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
    guilds = []
    try:
        conn, loop = get_db()
        cursor = loop.run_until_complete(conn.cursor(aiomysql.DictCursor))
        loop.run_until_complete(cursor.execute("SELECT * FROM guild_settings ORDER BY guild_id"))
        guilds = loop.run_until_complete(cursor.fetchall())
        conn.close()
        loop.close()
    except Exception as e:
        flash(f"DB error: {e}", "error")

    rows = ""
    for g in guilds:
        wc = g.get("welcome_channel_id") or "—"
        rows += f"""<tr class="guild-row">
          <td>{g['guild_id']}</td><td>{wc}</td><td>{'Yes' if g.get('welcome_embed') else 'No'}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="3" style="color:#72767d">No guild settings found</td></tr>'

    body = f"""
    <h1>Dashboard</h1>
    <div class="card">
      <h2>Controls</h2>
      <div style="display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:0.5rem">
        <form method="post" action="/api/restart"><button class="btn btn-danger">Restart Bot</button></form>
      </div>
    </div>
    <div class="card">
      <h2>Guild Settings</h2>
      <table>
        <tr><th>Guild ID</th><th>Welcome Channel</th><th>Custom Embed</th></tr>
        {rows}
      </table>
    </div>
    """
    return render_page("Dashboard", body, "dashboard")


@app.route("/welcome-editor", methods=["GET", "POST"])
@login_required
def welcome_editor():
    guild_id = request.args.get("guild_id") or request.form.get("guild_id", "")
    settings = None
    if guild_id:
        try:
            conn, loop = get_db()
            cursor = loop.run_until_complete(conn.cursor(aiomysql.DictCursor))
            loop.run_until_complete(
                cursor.execute("SELECT * FROM guild_settings WHERE guild_id = %s", (int(guild_id),))
            )
            settings = loop.run_until_complete(cursor.fetchone())
            conn.close()
            loop.close()
        except Exception:
            pass

    if request.method == "POST" and guild_id:
        welcome_channel = request.form.get("welcome_channel_id", "").strip()
        welcome_message = request.form.get("welcome_message", "").strip()
        embed_title = request.form.get("embed_title", "").strip()
        embed_description = request.form.get("embed_description", "").strip()
        embed_color = request.form.get("embed_color", "5865F2").strip()
        embed_thumbnail = request.form.get("embed_thumbnail", "").strip()
        embed_footer = request.form.get("embed_footer", "").strip()

        embed_data = None
        if embed_title or embed_description:
            try:
                c = int(embed_color.replace("#", ""), 16)
            except ValueError:
                c = 0x5865F2
            embed_data = {
                "title": embed_title,
                "description": embed_description,
                "color": c,
            }
            if embed_thumbnail:
                embed_data["thumbnail"] = {"url": embed_thumbnail}
            if embed_footer:
                embed_data["footer"] = {"text": embed_footer}

        try:
            conn, loop = get_db()
            cursor = loop.run_until_complete(conn.cursor())
            loop.run_until_complete(cursor.execute(
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
            ))
            conn.close()
            loop.close()
            flash("Welcome settings saved", "success")
        except Exception as e:
            flash(f"DB error: {e}", "error")

        return redirect(f"/welcome-editor?guild_id={guild_id}")

    wc = settings.get("welcome_channel_id", "") if settings else ""
    wm = settings.get("welcome_message", "") if settings else ""
    embed = {}
    if settings and settings.get("welcome_embed"):
        try:
            embed = settings["welcome_embed"] if isinstance(settings["welcome_embed"], dict) else json.loads(settings["welcome_embed"])
        except Exception:
            pass

    body = f"""
    <h1>Welcome Embed Editor</h1>
    <form method="post">
      <div class="card">
        <h2>Select Guild</h2>
        <label>Guild ID</label>
        <input type="number" name="guild_id" value="{guild_id}" placeholder="Discord Guild ID" required>
        <button class="btn btn-primary" type="submit" style="margin-top:0.25rem">Load</button>
      </div>
    """

    if guild_id:
        body += f"""
      <div class="card">
        <h2>Channel</h2>
        <label>Welcome Channel ID</label>
        <input type="number" name="welcome_channel_id" value="{wc or ''}" placeholder="Channel ID">
        <label>Default Welcome Message (used if no embed)</label>
        <input type="text" name="welcome_message" value="{wm or ''}" placeholder="Welcome {{user.mention}} to {{guild.name}}!">
      </div>
      <div class="card">
        <h2>Embed Settings</h2>
        <label>Title</label>
        <input type="text" name="embed_title" value="{embed.get('title', '')}" placeholder="Welcome!">
        <label>Description</label>
        <textarea name="embed_description" placeholder="Use {{user.mention}}, {{user.name}}, {{guild.name}}">{embed.get('description', '')}</textarea>
        <label>Color (hex)</label>
        <input type="text" name="embed_color" value="#{format(embed.get('color', 0x5865F2), '06x')}" placeholder="5865F2">
        <label>Thumbnail URL</label>
        <input type="text" name="embed_thumbnail" value="{embed.get('thumbnail', {}).get('url', '') if isinstance(embed.get('thumbnail'), dict) else ''}" placeholder="https://...">
        <label>Footer Text</label>
        <input type="text" name="embed_footer" value="{embed.get('footer', {}).get('text', '') if isinstance(embed.get('footer'), dict) else ''}" placeholder="Thanks for joining!">
        <button class="btn btn-success" type="submit" style="margin-top:0.5rem">Save Settings</button>
      </div>
      """
    body += "</form>"
    return render_page("Welcome Editor", body, "welcome")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/restart", methods=["POST"])
@login_required
def api_restart():
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", f"http://127.0.0.1:{BOT_PORT}/restart"],
            capture_output=True, timeout=5,
        )
        flash("Bot restart signal sent", "success")
    except Exception as e:
        flash(f"Restart failed: {e}", "error")
    return redirect(url_for("dashboard"))


@app.route("/api/status")
def api_status():
    stats = get_bot_stats()
    return stats


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
