#!/usr/bin/env python3
"""Solaxy Node Monitoring Dashboard"""

import json
import os
import re
import shutil
import subprocess
import time
import threading
from functools import lru_cache

import psycopg2
import requests
from flask import Flask, jsonify, render_template, request

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
    """Background thread: check services every 60s and send alerts on state changes."""
    import socket
    hostname = socket.gethostname()
    services = ["solaxy-node", "celestia-light", "postgresql"]

    while True:
        time.sleep(60)
        cfg = telegram_load_config()
        if not cfg.get("enabled") or not cfg.get("chat_id"):
            continue

        for svc in services:
            active = run_cmd(f"systemctl is-active {svc}") == "active"
            with _service_states_lock:
                prev = _service_states.get(svc)
                _service_states[svc] = active

            if prev is None:
                # First check — just record state, don't alert
                continue
            if prev and not active:
                telegram_send(f"⚠️ Service {svc} is DOWN on {hostname}")
            elif not prev and active:
                telegram_send(f"✅ Service {svc} is back UP on {hostname}")


def _telegram_build_health():
    """Build a health status text from current service states."""
    import socket
    hostname = socket.gethostname()

    services = [
        ("solaxy-node", "solaxy-node"),
        ("celestia-light", "celestia-light"),
        ("postgresql", "postgresql"),
    ]
    lines = [f"📊 Health — {hostname}", ""]
    for label, svc in services:
        status = run_cmd(f"systemctl is-active {svc}")
        icon = "✅" if status == "active" else "❌"
        lines.append(f"{icon} {label}: {status}")

    # Uptime
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            lines.append(f"\n⏱ Uptime: {days}d {hours}h {mins}m")
    except Exception:
        pass

    # CPU / Memory
    try:
        with open("/proc/loadavg") as f:
            load1 = f.read().split()[0]
            lines.append(f"🖥 CPU Load (1m): {load1}")
    except Exception:
        pass

    mem_raw = run_cmd("free -b | grep Mem")
    if mem_raw:
        parts = mem_raw.split()
        if len(parts) >= 7:
            used_gb = round(int(parts[2]) / 1024**3, 1)
            total_gb = round(int(parts[1]) / 1024**3, 1)
            pct = round(int(parts[2]) / int(parts[1]) * 100, 1)
            lines.append(f"🧠 Memory: {used_gb}G / {total_gb}G ({pct}%)")

    # Disk
    import shutil as _shutil
    disk = _shutil.disk_usage("/")
    disk_pct = round(disk.used / disk.total * 100, 1)
    lines.append(f"💾 Disk: {round(disk.used / 1024**3)}G / {round(disk.total / 1024**3)}G ({disk_pct}%)")

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
        return f"📋 No logs for {label} on {hostname}"
    # Telegram has a 4096 char limit — truncate if needed
    header = f"📋 Last 20 lines — {label} @ {hostname}\n\n"
    max_len = 4096 - len(header)
    if len(raw) > max_len:
        raw = raw[-max_len:]
    return header + raw


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

                if cmd == "/health":
                    telegram_send_to(chat_id, _telegram_build_health())
                elif cmd.startswith("/log"):
                    parts = text.split(None, 1)
                    service = parts[1] if len(parts) > 1 else "solaxy"
                    telegram_send_to(chat_id, _telegram_build_log(service))
                elif cmd in ("/start", "/help"):
                    welcome = (
                        "👋 Welcome to Solaxy Node Bot!\n\n"
                        "Available commands:\n\n"
                        "/health — Service status & system stats\n"
                        "/log — Last 20 solaxy-node log lines\n"
                        "/log celestia — Last 20 celestia log lines\n"
                        "/log postgresql — Last 20 postgresql log lines\n"
                        "/help — Show this message"
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
    # If we have synced height but no target, node is caught up — set target = synced
    if "synced_da_height" in info and "target_da_height" not in info:
        info["target_da_height"] = info["synced_da_height"]
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
    })


@app.route("/api/telegram/connect", methods=["POST"])
def api_telegram_connect():
    """Step 1: Generate a unique code and return a deep link for the user."""
    import secrets
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
    ok, msg = telegram_send(f"🔔 Test alert from {hostname} — Solaxy Node Dashboard")
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/telegram/toggle", methods=["POST"])
def api_telegram_toggle():
    cfg = telegram_load_config()
    cfg["enabled"] = not cfg.get("enabled", False)
    telegram_save_config(cfg)
    return jsonify({"ok": True, "enabled": cfg["enabled"]})


# Register bot commands in the Telegram menu
try:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
        json={"commands": [
            {"command": "health", "description": "Service status & system stats"},
            {"command": "log", "description": "Last 20 solaxy-node log lines"},
            {"command": "help", "description": "Show available commands"},
        ]},
        timeout=10,
    )
except Exception:
    pass

# Start Telegram background threads
_alert_thread = threading.Thread(target=_telegram_alert_loop, daemon=True)
_alert_thread.start()
_cmd_thread = threading.Thread(target=_telegram_command_loop, daemon=True)
_cmd_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, debug=False)
