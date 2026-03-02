#!/usr/bin/env python3
"""Solaxy Node Monitoring Dashboard"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import threading
from functools import lru_cache

import psycopg2
import requests
from flask import Flask, jsonify, render_template, request, make_response

try:
    import tomllib
except ImportError:
    import tomli as tomllib

app = Flask(__name__)

# Cache for expensive CLI calls
_cache = {}
_cache_lock = threading.Lock()

CELESTIA_STORE = os.path.expanduser("~/.celestia-light/")
CONFIG_PATH = os.path.expanduser("~/svm-rollup/config.toml")
GENESIS_CHAIN_STATE = os.path.expanduser("~/svm-rollup/genesis/chain_state_zk.json")

# ---------------------------------------------------------------------------
# Telegram Alert Integration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8693879678:AAEe0QnBTWczAkJnQfAuxFcVCn1s1iOEgWs"
TELEGRAM_BOT_USERNAME = "solaxynodebot"
TELEGRAM_CONFIG_PATH = os.path.expanduser("~/dashboard/telegram.json")

# In-memory last-known service states for alert transitions
_service_states = {}
_service_states_lock = threading.Lock()

# Pending connect code (generated per connect attempt)
_pending_connect_code = None

# ---------------------------------------------------------------------------
# SQLite Database for Uptime / Balance / Metrics History
# ---------------------------------------------------------------------------
UPTIME_DB_PATH = os.path.expanduser("~/dashboard/uptime.db")


def _init_db():
    """Create uptime.db with tables if they don't exist."""
    conn = sqlite3.connect(UPTIME_DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS uptime_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        service TEXT NOT NULL,
        active INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS balance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        tia_balance REAL,
        solx_balance REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS metrics_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        cpu_percent REAL,
        memory_percent REAL,
        da_rate REAL
    )""")
    conn.commit()
    conn.close()


_init_db()

# ---------------------------------------------------------------------------
# Auto-Restart Rate Limiting
# ---------------------------------------------------------------------------
_auto_restart_attempts = {}  # {service: [(timestamp, ...)] }
_auto_restart_lock = threading.Lock()


def _can_auto_restart(service):
    """Check if auto-restart is allowed (max 2 per hour per service)."""
    now = time.time()
    with _auto_restart_lock:
        attempts = _auto_restart_attempts.get(service, [])
        # Remove attempts older than 1 hour
        attempts = [t for t in attempts if now - t < 3600]
        _auto_restart_attempts[service] = attempts
        if len(attempts) >= 2:
            return False
        attempts.append(now)
        _auto_restart_attempts[service] = attempts
        return True


# ---------------------------------------------------------------------------
# Dashboard Password Protection
# ---------------------------------------------------------------------------

def _hash_password(password):
    """Hash a password with SHA-256 + salt."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password, stored):
    """Verify a password against a stored salt:hash."""
    if not stored or ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


def _has_password():
    """Check if a dashboard password has been set."""
    cfg = telegram_load_config()
    return bool(cfg.get("dashboard_password"))


# Active session tokens (in-memory, survive until restart)
_sessions = set()

_LOGIN_STYLE = """*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'SF Mono','Cascadia Code','Fira Code',monospace;
display:flex;justify-content:center;align-items:center;min-height:100vh}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;width:360px;text-align:center}
.login-box h1{font-size:18px;color:#f0f6fc;margin-bottom:6px}
.login-box h2{font-size:13px;color:#8b949e;font-weight:normal;margin-bottom:24px}
.login-box input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;
padding:10px 12px;font-family:inherit;font-size:13px;margin-bottom:12px}
.login-box input:focus{border-color:#58a6ff;outline:none}
.login-box button{width:100%;background:#238636;color:#fff;border:none;border-radius:6px;padding:10px;
font-family:inherit;font-size:13px;font-weight:600;cursor:pointer}
.login-box button:hover{background:#2ea043}
.login-msg{font-size:12px;margin-top:12px;min-height:16px}
.login-msg.err{color:#f85149}
.login-msg.ok{color:#3fb950}"""

_SETUP_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Solaxy Dashboard - Setup</title>
<style>{_LOGIN_STYLE}</style></head><body>
<div class="login-box">
<h1>Solaxy Node Dashboard</h1>
<h2>Set a password to secure your dashboard</h2>
<form onsubmit="doSetup(event)">
<input type="password" id="pw1" placeholder="Password" autofocus>
<input type="password" id="pw2" placeholder="Confirm password">
<button type="submit">Set Password</button>
</form>
<div class="login-msg" id="msg"></div>
</div>
<script>
async function doSetup(e){{
  e.preventDefault();
  const pw1=document.getElementById('pw1').value;
  const pw2=document.getElementById('pw2').value;
  const msg=document.getElementById('msg');
  if(!pw1){{msg.textContent='Please enter a password';msg.className='login-msg err';return;}}
  if(pw1.length<4){{msg.textContent='Password must be at least 4 characters';msg.className='login-msg err';return;}}
  if(pw1!==pw2){{msg.textContent='Passwords do not match';msg.className='login-msg err';return;}}
  try{{
    const r=await fetch('/api/set-password',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{password:pw1}})}});
    const d=await r.json();
    if(d.ok){{document.cookie='dashboard_session='+d.session+';path=/;max-age=2592000;SameSite=Lax';window.location.reload();}}
    else{{msg.textContent=d.error||'Failed';msg.className='login-msg err';}}
  }}catch(ex){{msg.textContent='Error: '+ex.message;msg.className='login-msg err';}}
}}
</script></body></html>"""

_LOGIN_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Solaxy Dashboard - Login</title>
<style>{_LOGIN_STYLE}</style></head><body>
<div class="login-box">
<h1>Solaxy Node Dashboard</h1>
<h2>Enter your password</h2>
<form onsubmit="doLogin(event)">
<input type="password" id="pw" placeholder="Password" autofocus>
<button type="submit">Login</button>
</form>
<div class="login-msg" id="msg"></div>
</div>
<script>
async function doLogin(e){{
  e.preventDefault();
  const pw=document.getElementById('pw').value;
  const msg=document.getElementById('msg');
  if(!pw){{msg.textContent='Please enter your password';msg.className='login-msg err';return;}}
  try{{
    const r=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{password:pw}})}});
    const d=await r.json();
    if(d.ok){{document.cookie='dashboard_session='+d.session+';path=/;max-age=2592000;SameSite=Lax';window.location.reload();}}
    else{{msg.textContent=d.error||'Wrong password';msg.className='login-msg err';}}
  }}catch(ex){{msg.textContent='Error: '+ex.message;msg.className='login-msg err';}}
}}
</script></body></html>"""


@app.before_request
def _check_auth():
    """Check dashboard password on every request."""
    # Allow static files without auth
    if request.path.startswith("/static/"):
        return None
    # Allow login/setup API without auth
    if request.path in ("/api/login", "/api/set-password"):
        return None
    # No password set yet — show setup page (allow everything for first visit)
    if not _has_password():
        if request.path.startswith("/api/"):
            return None
        return make_response(_SETUP_PAGE, 200)
    # Check session cookie
    session_token = request.cookies.get("dashboard_session")
    if session_token and session_token in _sessions:
        return None
    # Not authenticated
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return make_response(_LOGIN_PAGE, 401)


@app.route("/api/set-password", methods=["POST"])
def api_set_password():
    """Set the dashboard password (first-time setup only)."""
    if _has_password():
        return jsonify({"ok": False, "error": "Password already set. Use change-password."}), 400
    data = request.get_json()
    pw = (data or {}).get("password", "")
    if len(pw) < 4:
        return jsonify({"ok": False, "error": "Password must be at least 4 characters"}), 400
    cfg = telegram_load_config()
    cfg["dashboard_password"] = _hash_password(pw)
    cfg.pop("dashboard_token", None)  # remove old token field if present
    telegram_save_config(cfg)
    # Create session
    session_token = secrets.token_hex(32)
    _sessions.add(session_token)
    return jsonify({"ok": True, "session": session_token})


@app.route("/api/login", methods=["POST"])
def api_login():
    """Log in with the dashboard password."""
    data = request.get_json()
    pw = (data or {}).get("password", "")
    cfg = telegram_load_config()
    if not _verify_password(pw, cfg.get("dashboard_password", "")):
        return jsonify({"ok": False, "error": "Wrong password"}), 401
    session_token = secrets.token_hex(32)
    _sessions.add(session_token)
    return jsonify({"ok": True, "session": session_token})


@app.route("/api/change-password", methods=["POST"])
def api_change_password():
    """Change the dashboard password (requires current password)."""
    data = request.get_json()
    old_pw = (data or {}).get("old_password", "")
    new_pw = (data or {}).get("new_password", "")
    cfg = telegram_load_config()
    if not _verify_password(old_pw, cfg.get("dashboard_password", "")):
        return jsonify({"ok": False, "error": "Current password is wrong"}), 401
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "New password must be at least 4 characters"}), 400
    cfg["dashboard_password"] = _hash_password(new_pw)
    telegram_save_config(cfg)
    # Invalidate all sessions, create new one
    _sessions.clear()
    session_token = secrets.token_hex(32)
    _sessions.add(session_token)
    return jsonify({"ok": True, "session": session_token})


def telegram_load_config():
    """Load Telegram config from disk."""
    try:
        with open(TELEGRAM_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def telegram_save_config(cfg):
    """Save Telegram config to disk."""
    with open(TELEGRAM_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


def telegram_send_to(chat_id, text, parse_mode=None):
    """Send a message to a specific chat_id via the Telegram bot."""
    try:
        params = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            params=params,
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return True, "sent"
        return False, data.get("description", "unknown error")
    except Exception as e:
        return False, str(e)


def telegram_send(text):
    """Send a message to the configured chat_id."""
    cfg = telegram_load_config()
    chat_id = cfg.get("chat_id")
    if not chat_id:
        return False, "No chat_id configured"
    return telegram_send_to(chat_id, text)


def telegram_find_chat_by_code(code):
    """Search recent bot updates for a /start message containing the given code."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": -100},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            return None, data.get("description", "API error")
        results = data.get("result", [])
        if not results:
            return None, "No messages yet. Click the link and send /start to the bot first."
        # Search for /start <code> message
        for update in reversed(results):
            msg = update.get("message") or {}
            text = msg.get("text", "")
            if text == f"/start {code}":
                chat_id = str(msg["chat"]["id"])
                return chat_id, None
        return None, "Code not found. Click the link below and press START in Telegram, then click Confirm."
    except Exception as e:
        return None, str(e)


def _telegram_alert_loop():
    """Background thread: check services every 60s, log uptime/metrics, auto-restart."""
    import socket
    hostname = socket.gethostname()
    services = ["solaxy-node", "celestia-light", "postgresql"]

    while True:
        time.sleep(60)
        cfg = telegram_load_config()
        now = time.time()

        # Check service states and log to DB
        for svc in services:
            active = run_cmd(f"systemctl is-active {svc}") == "active"

            # Write to uptime_log
            try:
                conn = sqlite3.connect(UPTIME_DB_PATH)
                conn.execute(
                    "INSERT INTO uptime_log (timestamp, service, active) VALUES (?, ?, ?)",
                    (now, svc, 1 if active else 0),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

            with _service_states_lock:
                prev = _service_states.get(svc)
                _service_states[svc] = active

            if cfg.get("enabled") and cfg.get("chat_id"):
                if prev is None:
                    # First check -- just record state, don't alert
                    pass
                elif prev and not active:
                    telegram_send(f"\u26a0\ufe0f Service {svc} is DOWN on {hostname}")
                    # Auto-restart if enabled
                    if cfg.get("auto_restart") and _can_auto_restart(svc):
                        svc_unit = ALLOWED_SERVICES.get(svc, f"{svc}.service")
                        result = run_cmd(f"sudo systemctl restart {svc_unit}", timeout=30)
                        new_status = run_cmd(f"systemctl is-active {svc}")
                        if new_status == "active":
                            telegram_send(f"\u2705 Auto-restart: {svc} restarted successfully on {hostname}")
                            with _service_states_lock:
                                _service_states[svc] = True
                        else:
                            telegram_send(f"\u274c Auto-restart: {svc} restart FAILED on {hostname}")
                elif not prev and active:
                    telegram_send(f"\u2705 Service {svc} is back UP on {hostname}")

        # Log CPU/Memory/DA-Rate to metrics_log
        try:
            cpu_pct = None
            with open("/proc/stat") as f:
                line = f.readline().split()
                total = sum(int(x) for x in line[1:])
                idle = int(line[4])
                cpu_pct = round((1 - idle / total) * 100, 1)
        except Exception:
            pass

        mem_pct = None
        mem_raw = run_cmd("free -b | grep Mem")
        if mem_raw:
            parts = mem_raw.split()
            if len(parts) >= 7:
                mem_pct = round(int(parts[2]) / int(parts[1]) * 100, 1)

        da_rate = None
        with _block_stats_lock:
            if _block_stats["da_blocks_per_sec"] is not None:
                da_rate = _block_stats["da_blocks_per_sec"]

        try:
            conn = sqlite3.connect(UPTIME_DB_PATH)
            conn.execute(
                "INSERT INTO metrics_log (timestamp, cpu_percent, memory_percent, da_rate) VALUES (?, ?, ?, ?)",
                (now, cpu_pct, mem_pct, da_rate),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        # Auto-prune: delete entries older than 7 days
        cutoff = now - 7 * 86400
        try:
            conn = sqlite3.connect(UPTIME_DB_PATH)
            conn.execute("DELETE FROM uptime_log WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM balance_log WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM metrics_log WHERE timestamp < ?", (cutoff,))
            conn.commit()
            conn.close()
        except Exception:
            pass


def _balance_record_loop():
    """Background thread: record TIA + SOLX balance every 5 minutes."""
    while True:
        time.sleep(300)
        try:
            info = node_identity()
            tia_raw = info.get("tia_balance", "0")
            tia = int(tia_raw) / 1e6 if tia_raw else 0
            solx_raw = info.get("solx_balance", "0")
            solx = int(solx_raw) / 1e6 if solx_raw else 0

            conn = sqlite3.connect(UPTIME_DB_PATH)
            conn.execute(
                "INSERT INTO balance_log (timestamp, tia_balance, solx_balance) VALUES (?, ?, ?)",
                (time.time(), tia, solx),
            )
            conn.commit()
            conn.close()

            # Check TIA low threshold alert
            cfg = telegram_load_config()
            threshold = cfg.get("tia_low_threshold", 0.5)
            if cfg.get("enabled") and cfg.get("chat_id") and tia < threshold and tia > 0:
                telegram_send(
                    f"\u26a0\ufe0f TIA balance is low: {tia:.4f} TIA (threshold: {threshold})"
                )
        except Exception:
            pass


def _telegram_build_health():
    """Build a health status text from current service states."""
    import socket
    hostname = socket.gethostname()

    services = [
        ("solaxy-node", "solaxy-node"),
        ("celestia-light", "celestia-light"),
        ("postgresql", "postgresql"),
    ]
    lines = [f"\ud83d\udcca Health \u2014 {hostname}", ""]
    for label, svc in services:
        status = run_cmd(f"systemctl is-active {svc}")
        icon = "\u2705" if status == "active" else "\u274c"
        lines.append(f"{icon} {label}: {status}")

    # Uptime
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            lines.append(f"\nUptime: {days}d {hours}h {mins}m")
    except Exception:
        pass

    # CPU / Memory
    try:
        with open("/proc/loadavg") as f:
            load1 = f.read().split()[0]
            lines.append(f"CPU Load (1m): {load1}")
    except Exception:
        pass

    mem_raw = run_cmd("free -b | grep Mem")
    if mem_raw:
        parts = mem_raw.split()
        if len(parts) >= 7:
            used_gb = round(int(parts[2]) / 1024**3, 1)
            total_gb = round(int(parts[1]) / 1024**3, 1)
            pct = round(int(parts[2]) / int(parts[1]) * 100, 1)
            lines.append(f"Memory: {used_gb}G / {total_gb}G ({pct}%)")

    # Disk
    import shutil as _shutil
    disk = _shutil.disk_usage("/")
    disk_pct = round(disk.used / disk.total * 100, 1)
    lines.append(f"Disk: {round(disk.used / 1024**3)}G / {round(disk.total / 1024**3)}G ({disk_pct}%)")

    return "\n".join(lines)


def _telegram_build_log(service="solaxy"):
    """Build last 20 log lines for a service."""
    import socket
    hostname = socket.gethostname()
    svc_map = {
        "solaxy": "solaxy-node.service",
        "celestia": "celestia-light.service",
        "postgresql": "postgresql@16-main.service",
    }
    svc = svc_map.get(service, svc_map["solaxy"])
    label = service if service in svc_map else "solaxy"
    raw = run_cmd(f"journalctl -u {svc} -n 20 --no-pager -o short-iso 2>/dev/null")
    if not raw:
        return f"No logs for {label} on {hostname}"
    # Telegram has a 4096 char limit -- truncate if needed
    header = f"Last 20 lines -- {label} @ {hostname}\n\n"
    max_len = 4096 - len(header)
    if len(raw) > max_len:
        raw = raw[-max_len:]
    return header + raw


def _telegram_build_balance():
    """Build balance info text for /balance command."""
    info = node_identity()
    tia_raw = info.get("tia_balance", "0")
    tia = int(tia_raw) / 1e6 if tia_raw else 0
    solx_raw = info.get("solx_balance", "0")
    solx = int(solx_raw) / 1e6 if solx_raw else 0

    lines = [f"\ud83d\udcb0 TIA: {tia:.4f}", f"\ud83e\ude99 SOLX: {solx:.2f}"]

    # 24h delta
    try:
        conn = sqlite3.connect(UPTIME_DB_PATH)
        c = conn.cursor()
        cutoff = time.time() - 86400
        c.execute(
            "SELECT tia_balance, solx_balance FROM balance_log WHERE timestamp > ? ORDER BY timestamp ASC LIMIT 1",
            (cutoff,),
        )
        row = c.fetchone()
        conn.close()
        if row:
            tia_delta = tia - row[0]
            solx_delta = solx - row[1]
            lines.append(f"\n24h Change:")
            lines.append(f"  TIA: {tia_delta:+.4f}")
            lines.append(f"  SOLX: {solx_delta:+.2f}")
    except Exception:
        pass

    return "\n".join(lines)


# Offset tracker for the command polling loop
_tg_cmd_offset = 0


def _telegram_command_loop():
    """Background thread: poll for incoming Telegram commands and reply."""
    global _tg_cmd_offset

    while True:
        time.sleep(3)
        cfg = telegram_load_config()
        chat_id = cfg.get("chat_id")
        if not chat_id:
            continue

        try:
            params = {"timeout": 0}
            if _tg_cmd_offset:
                params["offset"] = _tg_cmd_offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=10,
            )
            data = r.json()
            if not data.get("ok"):
                continue
            for update in data.get("result", []):
                _tg_cmd_offset = update["update_id"] + 1
                msg = update.get("message") or {}
                msg_chat = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                # Only respond to the connected chat
                if msg_chat != chat_id:
                    continue

                cmd = text.split("@")[0]  # strip @botname suffix
                parts = cmd.split(None, 1)
                base_cmd = parts[0] if parts else ""
                cmd_arg = parts[1] if len(parts) > 1 else ""

                if base_cmd == "/health":
                    telegram_send_to(chat_id, _telegram_build_health())

                elif base_cmd == "/log":
                    service = cmd_arg if cmd_arg else "solaxy"
                    telegram_send_to(chat_id, _telegram_build_log(service))

                elif base_cmd == "/balance":
                    telegram_send_to(chat_id, _telegram_build_balance())

                elif base_cmd == "/autorestart":
                    cfg = telegram_load_config()
                    cfg["auto_restart"] = not cfg.get("auto_restart", False)
                    telegram_save_config(cfg)
                    status = "\u2705 ON" if cfg["auto_restart"] else "\u274c OFF"
                    telegram_send_to(chat_id, f"\ud83d\udd04 Auto-restart is now {status}")

                elif base_cmd in ("/restart", "/stop") and cmd_arg:
                    # Remote service control
                    svc_name = cmd_arg.strip()
                    if svc_name in ALLOWED_SERVICES:
                        action = base_cmd.lstrip("/")
                        svc_unit = ALLOWED_SERVICES[svc_name]
                        run_cmd(f"sudo systemctl {action} {svc_unit}", timeout=30)
                        new_status = run_cmd(f"systemctl is-active {svc_name}")
                        icon = "\u2705" if new_status == "active" else "\u274c"
                        telegram_send_to(
                            chat_id,
                            f"{icon} {action.title()}: {svc_name} \u2192 {new_status}",
                        )
                    else:
                        avail = ", ".join(ALLOWED_SERVICES.keys())
                        telegram_send_to(chat_id, f"Unknown service: {svc_name}\nAvailable: {avail}")

                elif base_cmd == "/start" and cmd_arg:
                    # /start <service> — remote start (not /start bare which is welcome)
                    svc_name = cmd_arg.strip()
                    if svc_name in ALLOWED_SERVICES:
                        svc_unit = ALLOWED_SERVICES[svc_name]
                        run_cmd(f"sudo systemctl start {svc_unit}", timeout=30)
                        new_status = run_cmd(f"systemctl is-active {svc_name}")
                        icon = "\u2705" if new_status == "active" else "\u274c"
                        telegram_send_to(chat_id, f"{icon} Start: {svc_name} \u2192 {new_status}")
                    else:
                        avail = ", ".join(ALLOWED_SERVICES.keys())
                        telegram_send_to(chat_id, f"Unknown service: {svc_name}\nAvailable: {avail}")

                elif base_cmd == "/update":
                    telegram_send_to(chat_id, "\ud83d\udd04 Updating dashboard...")
                    try:
                        git_out = run_cmd("cd ~/SolaxyEasyNode && git pull", timeout=30)
                        # Copy files
                        run_cmd("cp -r ~/SolaxyEasyNode/dashboard/* ~/dashboard/", timeout=10)
                        telegram_send_to(chat_id, f"\u2705 Update done:\n{git_out}\n\n\ud83d\udd04 Restarting dashboard...")
                        run_cmd("sudo systemctl restart solaxy-dashboard.service", timeout=15)
                    except Exception as e:
                        telegram_send_to(chat_id, f"\u274c Update failed: {e}")

                elif base_cmd in ("/start", "/help"):
                    welcome = (
                        "\ud83e\udd16 Solaxy Node Bot\n\n"
                        "Available commands:\n\n"
                        "\ud83d\udcca /health \u2014 Service status & system stats\n"
                        "\ud83d\udcc4 /log \u2014 Last 20 solaxy-node log lines\n"
                        "\ud83d\udcc4 /log celestia \u2014 Celestia log lines\n"
                        "\ud83d\udcc4 /log postgresql \u2014 PostgreSQL log lines\n"
                        "\ud83d\udcb0 /balance \u2014 TIA & SOLX balance + 24h delta\n"
                        "\ud83d\udd04 /restart <svc> \u2014 Restart a service\n"
                        "\u25b6\ufe0f /start <svc> \u2014 Start a service\n"
                        "\u23f9\ufe0f /stop <svc> \u2014 Stop a service\n"
                        "\ud83d\udee1\ufe0f /autorestart \u2014 Toggle auto-restart\n"
                        "\u2b06\ufe0f /update \u2014 Pull & update dashboard\n"
                        "\u2753 /help \u2014 Show this message"
                    )
                    telegram_send_to(chat_id, welcome)
        except Exception:
            pass


def get_genesis_da_height():
    """Read genesis_da_height from chain_state_zk.json."""
    try:
        with open(GENESIS_CHAIN_STATE) as f:
            return json.load(f).get("genesis_da_height", 0)
    except Exception:
        return 0


def cached(key, ttl=15):
    """Simple TTL cache decorator."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            with _cache_lock:
                if key in _cache:
                    val, ts = _cache[key]
                    if time.time() - ts < ttl:
                        return val
            result = fn(*args, **kwargs)
            with _cache_lock:
                _cache[key] = (result, time.time())
            return result
        return wrapper
    return decorator


def run_cmd(cmd, timeout=10):
    """Run a shell command and return stdout."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def systemd_status(service):
    """Get systemd service status."""
    active = run_cmd(f"systemctl is-active {service}")
    props = run_cmd(
        f"systemctl show {service} --property=MemoryCurrent,CPUUsageNSec,ActiveEnterTimestamp"
    )
    info = {"active": active == "active", "status": active}
    for line in props.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k == "MemoryCurrent" and v.isdigit():
                info["memory_mb"] = round(int(v) / 1024 / 1024)
            elif k == "CPUUsageNSec" and v.isdigit():
                info["cpu_seconds"] = round(int(v) / 1e9, 1)
            elif k == "ActiveEnterTimestamp":
                info["started"] = v
    return info


def _rpc_call(url, method, params=None, timeout=5):
    """Make a JSON-RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        data = r.json()
        return data.get("result")
    except Exception:
        return None


LOCAL_RPC = "http://127.0.0.1:8080"

# Block / DA processing rate measurement
_block_stats = {
    "last_slot": None, "last_slot_time": None,
    "block_time_ms": None, "slots_per_sec": None,
    "last_da": None, "last_da_time": None,
    "da_blocks_per_sec": None,
}
_block_stats_lock = threading.Lock()


def _block_time_loop():
    """Background thread: measure slot + DA block processing rates."""
    while True:
        time.sleep(10)
        now = time.time()

        # Measure SVM slot rate via RPC
        slot = _rpc_call(LOCAL_RPC, "getSlot")

        # Measure DA height rate from latest journal line
        da_line = run_cmd(
            "journalctl -u solaxy-node.service -n 20 --no-pager 2>/dev/null"
            " | grep -oP 'fork_point_height=\\K\\d+' | tail -1"
        )
        da_height = int(da_line) if da_line and da_line.isdigit() else None

        with _block_stats_lock:
            # SVM slots
            if slot is not None:
                prev_slot = _block_stats["last_slot"]
                prev_time = _block_stats["last_slot_time"]
                _block_stats["last_slot"] = slot
                _block_stats["last_slot_time"] = now
                if prev_slot is not None and prev_time is not None:
                    dt = now - prev_time
                    ds = slot - prev_slot
                    if dt > 0 and ds > 0:
                        _block_stats["slots_per_sec"] = round(ds / dt, 1)
                        _block_stats["block_time_ms"] = round(dt / ds * 1000, 1)
                    elif ds == 0:
                        _block_stats["slots_per_sec"] = 0
                        _block_stats["block_time_ms"] = None

            # DA blocks -- only update rate when height actually changed
            if da_height is not None:
                prev_da = _block_stats["last_da"]
                prev_da_time = _block_stats["last_da_time"]
                if prev_da is None or da_height != prev_da:
                    _block_stats["last_da"] = da_height
                    _block_stats["last_da_time"] = now
                    if prev_da is not None and prev_da_time is not None and da_height > prev_da:
                        dt = now - prev_da_time
                        dd = da_height - prev_da
                        if dt > 0:
                            _block_stats["da_blocks_per_sec"] = round(dd / dt, 2)


def parse_solaxy_logs():
    """Parse recent solaxy logs for sync progress and slot info."""
    lines = run_cmd("journalctl -u solaxy-node.service -n 500 --no-pager 2>/dev/null")
    info = {}
    for line in reversed(lines.splitlines()):
        if "synced_da_height=" in line and "synced_da_height" not in info:
            m = re.search(r"synced_da_height=(\d+)\s+target_da_height=(\d+)", line)
            if m:
                info["synced_da_height"] = int(m.group(1))
                info["target_da_height"] = int(m.group(2))
        if "next_da_height=" in line and "synced_da_height" not in info:
            m = re.search(r"next_da_height=(\d+)", line)
            if m:
                info["synced_da_height"] = int(m.group(1))
        if "slot_number=" in line and "slot_number" not in info:
            m = re.search(r"slot_number=(\d+)", line)
            if m:
                info["slot_number"] = int(m.group(1))
        if "Block execution complete time=" in line and "block_time_ms" not in info:
            m = re.search(r"time=(\d+(?:\.\d+)?)ms", line)
            if m:
                info["block_time_ms"] = float(m.group(1))
        if "is below Tail" in line and "waiting_for_celestia" not in info:
            m = re.search(r"requested header \((\d+)\) is below Tail \((\d+)\)", line)
            if m:
                info["waiting_for_celestia"] = True
                info["needed_height"] = int(m.group(1))
                info["celestia_tail"] = int(m.group(2))
        if "fork_point_height=" in line and "synced_da_height" not in info:
            m = re.search(r"fork_point_height=(\d+)", line)
            if m:
                info["synced_da_height"] = int(m.group(1))
                info["target_da_height"] = int(m.group(1))
        if len(info) >= 6:
            break
    # If we have synced height but no target, node is caught up -- set target = synced
    if "synced_da_height" in info and "target_da_height" not in info:
        info["target_da_height"] = info["synced_da_height"]
    # When synced, logs no longer contain slot_number -- fall back to local RPC
    if "slot_number" not in info:
        slot = _rpc_call(LOCAL_RPC, "getSlot")
        if slot is not None:
            info["slot_number"] = slot
    # Fall back to measured rates from background thread
    with _block_stats_lock:
        if "block_time_ms" not in info and _block_stats["block_time_ms"] is not None:
            info["block_time_ms"] = _block_stats["block_time_ms"]
        if _block_stats["slots_per_sec"] is not None:
            info["slots_per_sec"] = _block_stats["slots_per_sec"]
        if _block_stats["da_blocks_per_sec"] is not None:
            info["da_blocks_per_sec"] = _block_stats["da_blocks_per_sec"]
    return info


@cached("celestia_sync", ttl=10)
def celestia_sync_state():
    """Get Celestia sync state."""
    raw = run_cmd(f"celestia header sync-state --node.store {CELESTIA_STORE}")
    try:
        return json.loads(raw).get("result", {})
    except Exception:
        return {}


@cached("celestia_das", ttl=15)
def celestia_das_stats():
    """Get Celestia DAS sampling stats."""
    raw = run_cmd(f"celestia das sampling-stats --node.store {CELESTIA_STORE}")
    try:
        return json.loads(raw).get("result", {})
    except Exception:
        return {}


@cached("celestia_balance", ttl=30)
def celestia_balance():
    """Get Celestia wallet balance."""
    raw = run_cmd(f"celestia state balance --node.store {CELESTIA_STORE}")
    try:
        return json.loads(raw).get("result", {})
    except Exception:
        return {}


@cached("celestia_p2p", ttl=60)
def celestia_p2p():
    """Get Celestia P2P info."""
    raw = run_cmd(f"celestia p2p info --node.store {CELESTIA_STORE}")
    try:
        return json.loads(raw).get("result", {})
    except Exception:
        return {}


def db_stats():
    """Get PostgreSQL stats."""
    try:
        conn = psycopg2.connect(dbname="svm", user="postgres", password="secret", host="localhost")
        cur = conn.cursor()
        stats = {}
        for table in ("blocks", "transactions", "accounts"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            stats[f"{table}_count"] = cur.fetchone()[0]
        cur.execute("SELECT pg_size_pretty(pg_database_size('svm'))")
        stats["db_size"] = cur.fetchone()[0]
        cur.close()
        conn.close()
        stats["connected"] = True
        return stats
    except Exception as e:
        return {"connected": False, "error": str(e)}


def prometheus_stats():
    """Fetch Solaxy Prometheus metrics."""
    try:
        r = requests.get("http://127.0.0.1:9845/metrics", timeout=3)
        metrics = {}
        for line in r.text.splitlines():
            if line.startswith("#"):
                continue
            if "schemadb_batch_commit_bytes" in line or "rockbound_put_bytes" in line:
                parts = line.split()
                if len(parts) == 2:
                    metrics[parts[0]] = float(parts[1])
        return metrics
    except Exception:
        return {}


PUBLIC_RPC = "https://mainnet.rpc.solaxy.io"
SOLX_WALLET_PATH = os.path.expanduser("~/svm-rollup/node-wallet.json")


@cached("rpc_local", ttl=5)
def local_rpc_stats():
    """Get stats from local Solaxy RPC sidecar."""
    return {
        "slot": _rpc_call(LOCAL_RPC, "getSlot"),
        "block_height": _rpc_call(LOCAL_RPC, "getBlockHeight"),
        "tx_count": _rpc_call(LOCAL_RPC, "getTransactionCount"),
    }


@cached("rpc_public", ttl=10)
def public_rpc_stats():
    """Get stats from public Solaxy RPC for comparison."""
    result = {
        "slot": _rpc_call(PUBLIC_RPC, "getSlot"),
        "block_height": _rpc_call(PUBLIC_RPC, "getBlockHeight"),
        "tx_count": _rpc_call(PUBLIC_RPC, "getTransactionCount"),
    }
    epoch = _rpc_call(PUBLIC_RPC, "getEpochInfo")
    if epoch:
        result["epoch"] = epoch.get("epoch")
    return result


def system_stats():
    """Get comprehensive system-level stats."""
    # Disk
    disk = shutil.disk_usage("/")

    # Memory
    mem = {}
    mem_raw = run_cmd("free -b | grep Mem")
    if mem_raw:
        parts = mem_raw.split()
        if len(parts) >= 7:
            mem["total_gb"] = round(int(parts[1]) / 1024**3, 1)
            mem["used_gb"] = round(int(parts[2]) / 1024**3, 1)
            mem["available_gb"] = round(int(parts[6]) / 1024**3, 1)
            mem["percent"] = round(int(parts[2]) / int(parts[1]) * 100, 1)

    # CPU load
    load = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load["1m"] = float(parts[0])
            load["5m"] = float(parts[1])
            load["15m"] = float(parts[2])
    except Exception:
        pass

    # CPU usage per-core snapshot
    cpu_percent = None
    try:
        with open("/proc/stat") as f:
            line = f.readline().split()
            total = sum(int(x) for x in line[1:])
            idle = int(line[4])
            cpu_percent = round((1 - idle / total) * 100, 1)
    except Exception:
        pass

    # Temperatures
    temps = {}
    try:
        for hwmon in sorted(os.listdir("/sys/class/hwmon/")):
            base = f"/sys/class/hwmon/{hwmon}"
            name_path = os.path.join(base, "name")
            if os.path.exists(name_path):
                with open(name_path) as f:
                    name = f.read().strip()
                temp_path = os.path.join(base, "temp1_input")
                if os.path.exists(temp_path):
                    with open(temp_path) as f:
                        temps[name] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    # Disk I/O (sda)
    disk_io = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 14 and parts[2] == "sda":
                    disk_io["read_mb"] = round(int(parts[5]) * 512 / 1024**2)
                    disk_io["write_mb"] = round(int(parts[9]) * 512 / 1024**2)
                    break
    except Exception:
        pass

    # Network traffic
    net = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                line = line.strip()
                if line.startswith("wlp") or line.startswith("eth") or line.startswith("enp"):
                    parts = line.split()
                    iface = parts[0].rstrip(":")
                    rx = int(parts[1])
                    tx = int(parts[9])
                    net["interface"] = iface
                    net["rx_gb"] = round(rx / 1024**3, 2)
                    net["tx_gb"] = round(tx / 1024**3, 2)
                    if rx > 0 or tx > 0:
                        break
    except Exception:
        pass

    # Uptime
    uptime_str = ""
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            if days > 0:
                uptime_str = f"{days}d {hours}h {mins}m"
            else:
                uptime_str = f"{hours}h {mins}m"
    except Exception:
        pass

    return {
        "disk_total_gb": round(disk.total / 1024**3),
        "disk_used_gb": round(disk.used / 1024**3),
        "disk_free_gb": round(disk.free / 1024**3),
        "disk_percent": round(disk.used / disk.total * 100, 1),
        "memory": mem,
        "load": load,
        "cpu_percent": cpu_percent,
        "temps": temps,
        "disk_io": disk_io,
        "net": net,
        "uptime": uptime_str,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    solaxy_svc = systemd_status("solaxy-node.service")
    solaxy_sync = parse_solaxy_logs()
    celestia_svc = systemd_status("celestia-light.service")
    pg_svc = systemd_status("postgresql.service")

    return jsonify({
        "genesis_da_height": get_genesis_da_height(),
        "solaxy": {
            "service": solaxy_svc,
            "sync": solaxy_sync,
        },
        "celestia": {
            "service": celestia_svc,
            "sync": celestia_sync_state(),
            "das": celestia_das_stats(),
            "balance": celestia_balance(),
            "p2p": celestia_p2p(),
        },
        "postgresql": {
            "service": pg_svc,
            "db": db_stats(),
        },
        "rpc": {
            "local": local_rpc_stats(),
            "network": public_rpc_stats(),
        },
        "system": system_stats(),
        "timestamp": time.time(),
    })


@app.route("/api/logs/<service>")
def api_logs(service):
    service_map = {
        "solaxy": "solaxy-node.service",
        "celestia": "celestia-light.service",
        "postgresql": "postgresql@16-main.service",
    }
    svc = service_map.get(service)
    if not svc:
        return jsonify({"error": "unknown service"}), 404
    lines = int(request.args.get("lines", 100))
    lines = min(lines, 500)
    raw = run_cmd(f"journalctl -u {svc} -n {lines} --no-pager -o short-iso 2>/dev/null")
    return jsonify({"service": service, "lines": raw.splitlines()})


@cached("attester_info", ttl=30)
def attester_info():
    """Get attester incentives state from rollup REST API."""
    base = "http://127.0.0.1:8899/modules/attester-incentives/state"
    info = {}
    try:
        r = requests.get(f"{base}/maximum-attested-height", timeout=3)
        val = r.json().get("value")
        info["max_attested_height"] = val if val is not None else 0
    except Exception:
        info["max_attested_height"] = None
    try:
        r = requests.get(f"{base}/light-client-finalized-height", timeout=3)
        val = r.json().get("value")
        info["lc_finalized_height"] = val if val is not None else 0
    except Exception:
        info["lc_finalized_height"] = None
    # bonded_attesters is a state_map, no easy way to count entries via REST
    info["bonded_attesters"] = 0
    return info


@app.route("/api/attester-info")
def api_attester_info():
    return jsonify(attester_info())


@cached("node_identity", ttl=120)
def node_identity():
    """Get node identity info from Celestia."""
    info = {}
    # Peer ID
    raw = run_cmd(f"celestia p2p info --node.store {CELESTIA_STORE}")
    try:
        result = json.loads(raw).get("result", {})
        info["peer_id"] = result.get("id", "")
        # Extract public IP from peer addresses
        for addr in result.get("peer_addr", []):
            if addr.startswith("/ip4/") and not addr.startswith("/ip4/127.") and not addr.startswith("/ip4/192.168.") and not addr.startswith("/ip4/10."):
                ip = addr.split("/")[2]
                info["public_ip"] = ip
                break
    except Exception:
        pass
    # Wallet address
    raw = run_cmd(f"celestia state account-address --node.store {CELESTIA_STORE}")
    try:
        info["wallet"] = json.loads(raw).get("result", "")
    except Exception:
        pass
    # TIA balance
    raw = run_cmd(f"celestia state balance --node.store {CELESTIA_STORE}")
    try:
        info["tia_balance"] = json.loads(raw).get("result", {}).get("amount", "0")
    except Exception:
        info["tia_balance"] = "0"
    # Solaxy L2 wallet
    try:
        with open(SOLX_WALLET_PATH) as f:
            import base58
            key_bytes = bytes(json.load(f)[:32])
            # Derive public key from first 64 bytes (keypair = secret + public)
            full = json.load(open(SOLX_WALLET_PATH))
            pub_bytes = bytes(full[32:])
            info["solx_wallet"] = base58.b58encode(pub_bytes).decode()
    except Exception:
        # Fallback: use solana-keygen
        solana_bin = os.path.expanduser("~/.local/share/solana/install/active_release/bin/solana-keygen")
        pubkey = run_cmd(f"{solana_bin} pubkey {SOLX_WALLET_PATH}")
        info["solx_wallet"] = pubkey if pubkey else ""
    # SOLX balance via RPC
    if info.get("solx_wallet"):
        result = _rpc_call(PUBLIC_RPC, "getBalance", [info["solx_wallet"]])
        if result and isinstance(result, dict):
            info["solx_balance"] = str(result.get("value", 0))
        else:
            info["solx_balance"] = "0"
    return info


@app.route("/api/node-identity")
def api_node_identity():
    return jsonify(node_identity())


# ---------------------------------------------------------------------------
# Uptime History API
# ---------------------------------------------------------------------------

@app.route("/api/uptime")
def api_uptime():
    hours = int(request.args.get("hours", 24))
    cutoff = time.time() - hours * 3600
    services = ["solaxy-node", "celestia-light", "postgresql"]
    result = {}

    try:
        conn = sqlite3.connect(UPTIME_DB_PATH)
        c = conn.cursor()
        for svc in services:
            c.execute(
                "SELECT timestamp, active FROM uptime_log WHERE service = ? AND timestamp > ? ORDER BY timestamp ASC",
                (svc, cutoff),
            )
            rows = c.fetchall()
            total = len(rows)
            up = sum(1 for _, a in rows if a)
            pct = round(up / total * 100, 2) if total > 0 else 100.0
            timeline = [{"t": r[0], "up": bool(r[1])} for r in rows]
            result[svc] = {"uptime_pct": pct, "checks": total, "timeline": timeline}
        conn.close()
    except Exception:
        for svc in services:
            result[svc] = {"uptime_pct": 100.0, "checks": 0, "timeline": []}

    return jsonify(result)


# ---------------------------------------------------------------------------
# Balance History API
# ---------------------------------------------------------------------------

@app.route("/api/balance-history")
def api_balance_history():
    hours = int(request.args.get("hours", 24))
    cutoff = time.time() - hours * 3600

    try:
        conn = sqlite3.connect(UPTIME_DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT timestamp, tia_balance, solx_balance FROM balance_log WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,),
        )
        rows = c.fetchall()
        conn.close()

        entries = [{"t": r[0], "tia": r[1], "solx": r[2]} for r in rows]
        tia_delta = None
        solx_delta = None
        burn_rate = None
        if len(rows) >= 2:
            tia_delta = round(rows[-1][1] - rows[0][1], 6)
            solx_delta = round(rows[-1][2] - rows[0][2], 6)
            dt_hours = (rows[-1][0] - rows[0][0]) / 3600
            if dt_hours > 0 and tia_delta < 0:
                burn_rate = round(abs(tia_delta) / dt_hours * 24, 6)

        return jsonify({
            "entries": entries,
            "tia_delta_24h": tia_delta,
            "solx_delta_24h": solx_delta,
            "daily_burn_rate": burn_rate,
        })
    except Exception:
        return jsonify({"entries": [], "tia_delta_24h": None, "solx_delta_24h": None, "daily_burn_rate": None})


# ---------------------------------------------------------------------------
# Metrics History API (for Sparklines)
# ---------------------------------------------------------------------------

@app.route("/api/metrics-history")
def api_metrics_history():
    minutes = int(request.args.get("minutes", 60))
    cutoff = time.time() - minutes * 60

    try:
        conn = sqlite3.connect(UPTIME_DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT timestamp, cpu_percent, memory_percent, da_rate FROM metrics_log WHERE timestamp > ? ORDER BY timestamp ASC",
            (cutoff,),
        )
        rows = c.fetchall()
        conn.close()

        return jsonify({
            "timestamps": [r[0] for r in rows],
            "cpu": [r[1] for r in rows],
            "memory": [r[2] for r in rows],
            "da_rate": [r[3] for r in rows],
        })
    except Exception:
        return jsonify({"timestamps": [], "cpu": [], "memory": [], "da_rate": []})


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

def parse_config():
    """Read and parse config.toml, return dict."""
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        return {"_error": str(e)}


def write_config(data):
    """Write config dict back to config.toml, preserving structure."""
    lines = []

    def write_section(d, prefix=""):
        for key, val in d.items():
            if isinstance(val, dict):
                section = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
                # Skip empty sub-sections (just write the header)
                lines.append(f"\n[{section}]")
                write_section(val, section if not prefix else section)
            else:
                if isinstance(val, bool):
                    lines.append(f'{key} = {"true" if val else "false"}')
                elif isinstance(val, int):
                    lines.append(f"{key} = {val}")
                elif isinstance(val, float):
                    lines.append(f"{key} = {val}")
                else:
                    lines.append(f'{key} = "{val}"')

    # Write top-level sections in order
    section_order = ["da", "storage", "runner", "monitoring", "proof_manager", "sequencer"]
    written = set()

    for section in section_order:
        if section in data:
            lines.append(f"\n[{section}]")
            section_data = data[section]
            for key, val in section_data.items():
                if isinstance(val, dict):
                    # Sub-section like runner.http_config or sequencer.standard
                    lines.append(f"\n[{section}.{key}]")
                    for sk, sv in val.items():
                        if isinstance(sv, bool):
                            lines.append(f'{sk} = {"true" if sv else "false"}')
                        elif isinstance(sv, (int, float)):
                            lines.append(f"{sk} = {sv}")
                        else:
                            lines.append(f'{sk} = "{sv}"')
                else:
                    if isinstance(val, bool):
                        lines.append(f'{key} = {"true" if val else "false"}')
                    elif isinstance(val, (int, float)):
                        lines.append(f"{key} = {val}")
                    else:
                        lines.append(f'{key} = "{val}"')
            written.add(section)

    # Write any remaining sections
    for section, section_data in data.items():
        if section in written or section.startswith("_"):
            continue
        if isinstance(section_data, dict):
            lines.append(f"\n[{section}]")
            for key, val in section_data.items():
                if isinstance(val, dict):
                    lines.append(f"\n[{section}.{key}]")
                    for sk, sv in val.items():
                        if isinstance(sv, bool):
                            lines.append(f'{sk} = {"true" if sv else "false"}')
                        elif isinstance(sv, (int, float)):
                            lines.append(f"{sk} = {sv}")
                        else:
                            lines.append(f'{sk} = "{sv}"')
                else:
                    if isinstance(val, bool):
                        lines.append(f'{key} = {"true" if val else "false"}')
                    elif isinstance(val, (int, float)):
                        lines.append(f"{key} = {val}")
                    else:
                        lines.append(f'{key} = "{val}"')

    content = "\n".join(lines).strip() + "\n"
    with open(CONFIG_PATH, "w") as f:
        f.write(content)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(parse_config())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    restart = data.pop("_restart", False)

    try:
        write_config(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if restart:
        run_cmd("sudo systemctl restart solaxy-node.service", timeout=15)

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Service Control API
# ---------------------------------------------------------------------------

ALLOWED_SERVICES = {
    "solaxy-node": "solaxy-node.service",
    "celestia-light": "celestia-light.service",
    "postgresql": "postgresql.service",
    "solaxy-dashboard": "solaxy-dashboard.service",
}

ALLOWED_ACTIONS = {"start", "stop", "restart"}


@app.route("/api/service/<name>/<action>", methods=["POST"])
def api_service_control(name, action):
    if name not in ALLOWED_SERVICES:
        return jsonify({"error": f"unknown service: {name}"}), 404
    if action not in ALLOWED_ACTIONS:
        return jsonify({"error": f"unknown action: {action}"}), 400

    svc = ALLOWED_SERVICES[name]
    result = run_cmd(f"sudo systemctl {action} {svc}", timeout=30)
    status = run_cmd(f"systemctl is-active {svc}")

    return jsonify({"ok": True, "service": name, "action": action, "status": status})


# ---------------------------------------------------------------------------
# Telegram API Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/telegram")
def api_telegram():
    cfg = telegram_load_config()
    return jsonify({
        "connected": bool(cfg.get("chat_id")),
        "chat_id": cfg.get("chat_id", ""),
        "enabled": cfg.get("enabled", False),
        "auto_restart": cfg.get("auto_restart", False),
        "tia_low_threshold": cfg.get("tia_low_threshold", 0.5),
    })


@app.route("/api/telegram/connect", methods=["POST"])
def api_telegram_connect():
    """Step 1: Generate a unique code and return a deep link for the user."""
    global _pending_connect_code
    _pending_connect_code = secrets.token_hex(8)
    link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={_pending_connect_code}"
    return jsonify({"ok": True, "code": _pending_connect_code, "link": link})


@app.route("/api/telegram/connect/confirm", methods=["POST"])
def api_telegram_connect_confirm():
    """Step 2: Check if the user sent /start <code> to the bot."""
    global _pending_connect_code
    if not _pending_connect_code:
        return jsonify({"ok": False, "error": "No pending connect. Click Connect first."})
    chat_id, error = telegram_find_chat_by_code(_pending_connect_code)
    if error:
        return jsonify({"ok": False, "error": error})
    cfg = telegram_load_config()
    cfg["chat_id"] = chat_id
    cfg.setdefault("enabled", True)
    telegram_save_config(cfg)
    _pending_connect_code = None
    return jsonify({"ok": True, "chat_id": chat_id})


@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    import socket
    hostname = socket.gethostname()
    ok, msg = telegram_send(f"Test alert from {hostname} -- Solaxy Node Dashboard")
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/telegram/toggle", methods=["POST"])
def api_telegram_toggle():
    cfg = telegram_load_config()
    cfg["enabled"] = not cfg.get("enabled", False)
    telegram_save_config(cfg)
    return jsonify({"ok": True, "enabled": cfg["enabled"]})


@app.route("/api/telegram/auto-restart", methods=["POST"])
def api_telegram_auto_restart():
    cfg = telegram_load_config()
    cfg["auto_restart"] = not cfg.get("auto_restart", False)
    telegram_save_config(cfg)
    return jsonify({"ok": True, "auto_restart": cfg["auto_restart"]})


# ---------------------------------------------------------------------------
# One-Click Update API
# ---------------------------------------------------------------------------

@app.route("/api/update", methods=["POST"])
def api_update():
    """Git pull in ~/SolaxyEasyNode, copy files to ~/dashboard."""
    try:
        git_output = run_cmd("cd ~/SolaxyEasyNode && git pull", timeout=30)
        copy_output = run_cmd("cp -r ~/SolaxyEasyNode/dashboard/* ~/dashboard/", timeout=10)
        return jsonify({"ok": True, "git_output": git_output, "copy_output": copy_output})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update/restart", methods=["POST"])
def api_update_restart():
    """Restart the dashboard service."""
    try:
        run_cmd("sudo systemctl restart solaxy-dashboard.service", timeout=15)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Register bot commands in the Telegram menu
try:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
        json={"commands": [
            {"command": "health", "description": "Service status & system stats"},
            {"command": "log", "description": "Last 20 log lines (+ celestia/postgresql)"},
            {"command": "balance", "description": "TIA & SOLX balance + 24h delta"},
            {"command": "restart", "description": "Restart a service (e.g. /restart solaxy-node)"},
            {"command": "start", "description": "Start a service"},
            {"command": "stop", "description": "Stop a service"},
            {"command": "autorestart", "description": "Toggle auto-restart on/off"},
            {"command": "update", "description": "Pull & update dashboard"},
            {"command": "help", "description": "Show available commands"},
        ]},
        timeout=10,
    )
except Exception:
    pass

# Start background threads
_block_time_thread = threading.Thread(target=_block_time_loop, daemon=True)
_block_time_thread.start()
_alert_thread = threading.Thread(target=_telegram_alert_loop, daemon=True)
_alert_thread.start()
_cmd_thread = threading.Thread(target=_telegram_command_loop, daemon=True)
_cmd_thread.start()
_balance_thread = threading.Thread(target=_balance_record_loop, daemon=True)
_balance_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, debug=False)
