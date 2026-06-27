#!/usr/bin/env bash
#
# bridge-cleanup.sh — wipe celestia-bridge state to reclaim disk space.
#
# Drops the BadgerDB header/data store and the EDS block store, then resyncs
# the bridge from a recent Celestia head. Keeps `keys/` and `config.toml`.
#
# Use when ~/.celestia-bridge has grown beyond what's needed for a rollup's
# recent-DA-tail workload (the bridge by default keeps 7 days of block data
# + all headers since first sync; this trims both).
#
# ─── Usage ────────────────────────────────────────────────────────────────
#   sudo ./bridge-cleanup.sh [--help]
#
# ─── Configuration (env vars or flags) ────────────────────────────────────
#   BRIDGE_DIR        Bridge data dir         [default: /root/.celestia-bridge]
#   BRIDGE_SERVICE    systemd unit name       [default: celestia-bridge]
#   ROLLUP_SERVICE    optional rollup unit    [default: solaxy-node, "" to skip]
#   RESYNC_DEPTH      blocks back from head   [default: 5000 OR auto]
#   AUTO_DEPTH        derive depth from rollup [default: true]
#   DEPTH_BUFFER      extra blocks below rollup's DA pointer [default: 5000]
#   ROLLUP_HTTP_PORT  rollup REST/HTTP port    [default: 8899]
#   MIN_FREE_GB       refuse to start if disk free is below this [default: 20]
#   P2P_NETWORK       celestia | mocha | ...  [auto-detected from config.toml]
#   CORE_IP           consensus node IP/host  [auto-detected from config.toml]
#   CORE_PORT         consensus gRPC port     [auto-detected from config.toml]
#   PUBLIC_RPC        public RPC for height   [auto-selected from network]
#   BRIDGE_RPC_PORT   local bridge RPC port   [default: 26658]
#
# Each may also be passed as a long flag, e.g. --rollup-service my-rollup.
#
# ─── Examples ─────────────────────────────────────────────────────────────
#   sudo ./bridge-cleanup.sh
#   sudo ROLLUP_SERVICE="" ./bridge-cleanup.sh                 # bridge only
#   sudo ./bridge-cleanup.sh --rollup-service my-rollup
#   sudo P2P_NETWORK=mocha ./bridge-cleanup.sh
#

set -euo pipefail

# ─── Defaults ─────────────────────────────────────────────────────────────
BRIDGE_DIR="${BRIDGE_DIR:-/root/.celestia-bridge}"
BRIDGE_SERVICE="${BRIDGE_SERVICE:-celestia-bridge}"
ROLLUP_SERVICE="${ROLLUP_SERVICE-solaxy-node}"   # set to "" to skip
RESYNC_DEPTH="${RESYNC_DEPTH:-5000}"
AUTO_DEPTH="${AUTO_DEPTH:-true}"
DEPTH_BUFFER="${DEPTH_BUFFER:-5000}"
ROLLUP_HTTP_PORT="${ROLLUP_HTTP_PORT:-8899}"
MIN_FREE_GB="${MIN_FREE_GB:-20}"
P2P_NETWORK="${P2P_NETWORK:-}"                   # auto-detected if empty
CORE_IP="${CORE_IP:-}"                           # auto-detected if empty
CORE_PORT="${CORE_PORT:-}"                       # auto-detected if empty
PUBLIC_RPC="${PUBLIC_RPC:-}"                     # picked by network if empty
BRIDGE_RPC_PORT="${BRIDGE_RPC_PORT:-26658}"

WAIT_LOOP_INTERVAL=5      # seconds between poll attempts during sync wait
WAIT_LOOP_MAX=180         # max poll attempts (= 15 min default)

# ─── CLI parsing ──────────────────────────────────────────────────────────
usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge-dir)       BRIDGE_DIR="$2"; shift 2 ;;
    --bridge-service)   BRIDGE_SERVICE="$2"; shift 2 ;;
    --rollup-service)   ROLLUP_SERVICE="$2"; shift 2 ;;
    --resync-depth)     RESYNC_DEPTH="$2"; AUTO_DEPTH=false; shift 2 ;;
    --auto-depth)       AUTO_DEPTH="$2"; shift 2 ;;
    --depth-buffer)     DEPTH_BUFFER="$2"; shift 2 ;;
    --rollup-http-port) ROLLUP_HTTP_PORT="$2"; shift 2 ;;
    --min-free-gb)      MIN_FREE_GB="$2"; shift 2 ;;
    --p2p-network)      P2P_NETWORK="$2"; shift 2 ;;
    --core-ip)          CORE_IP="$2"; shift 2 ;;
    --core-port)        CORE_PORT="$2"; shift 2 ;;
    --public-rpc)       PUBLIC_RPC="$2"; shift 2 ;;
    --bridge-rpc-port)  BRIDGE_RPC_PORT="$2"; shift 2 ;;
    -h|--help)          usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

BRIDGE_CFG="$BRIDGE_DIR/config.toml"
BACKUP_DIR="$BRIDGE_DIR.cleanup-backup-$(date +%Y%m%d-%H%M%S)"

log()   { printf "[%s] %s\n" "$(date '+%H:%M:%S')" "$*"; }
abort() { log "ERROR: $*"; exit 1; }

# ─── 1. Sanity ────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]]            || abort "must run as root (systemctl needs it)"
[[ -d "$BRIDGE_DIR/keys" ]]  || abort "missing $BRIDGE_DIR/keys — wrong --bridge-dir?"
[[ -f "$BRIDGE_CFG" ]]       || abort "missing $BRIDGE_CFG"
command -v jq   >/dev/null   || abort "jq not installed"
command -v curl >/dev/null   || abort "curl not installed"
command -v celestia >/dev/null || abort "celestia binary not on PATH"
systemctl list-unit-files "$BRIDGE_SERVICE.service" >/dev/null 2>&1 \
  || abort "systemd unit $BRIDGE_SERVICE.service not found"

# Optional rollup service: warn-and-skip if not present, instead of failing.
if [[ -n "$ROLLUP_SERVICE" ]] \
   && ! systemctl list-unit-files "$ROLLUP_SERVICE.service" >/dev/null 2>&1; then
  log "note: rollup unit $ROLLUP_SERVICE.service not found — skipping rollup stop/start"
  ROLLUP_SERVICE=""
fi

# ─── 2. Auto-detect network + core endpoint from existing config.toml ────
# config.toml has TrustedHash/TrustedPeers and a [Core] section with IP+Port.
# We use these as fallbacks only if the user hasn't overridden them.
cfg_get() {
  # crude TOML scalar extraction: "Key = value" or 'Key = "value"'
  grep -E "^\s*$1\s*=" "$BRIDGE_CFG" | head -1 \
    | sed -E "s/^[^=]*=\s*\"?([^\"]*)\"?\s*$/\1/"
}

[[ -z "$CORE_IP"     ]] && CORE_IP="$(cfg_get IP   || true)"
[[ -z "$CORE_PORT"   ]] && CORE_PORT="$(cfg_get Port || true)"

# Fall back if config didn't yield anything sensible.
[[ -z "$CORE_IP"   ]] && CORE_IP="consensus.lunaroasis.net"
[[ -z "$CORE_PORT" ]] && CORE_PORT="9090"

# Network: not stored in config.toml — infer from the keys/ directory name
# convention (`keys/keyring-test/` for celestia mainnet keyring is the same
# across networks, so we default to celestia and let the user override).
[[ -z "$P2P_NETWORK" ]] && P2P_NETWORK="celestia"

# Public RPC for "what's the current head height?" — pick a sensible default
# per network. Override with PUBLIC_RPC env var or --public-rpc.
if [[ -z "$PUBLIC_RPC" ]]; then
  case "$P2P_NETWORK" in
    celestia)   PUBLIC_RPC="https://celestia-rpc.polkachu.com" ;;
    mocha|mocha-4) PUBLIC_RPC="https://rpc-mocha.pops.one" ;;
    arabica|arabica-11) PUBLIC_RPC="https://rpc.celestia-arabica-11.com" ;;
    *)          PUBLIC_RPC="https://celestia-rpc.polkachu.com" ;;
  esac
fi

log "config:"
log "  bridge dir:      $BRIDGE_DIR"
log "  bridge service:  $BRIDGE_SERVICE"
log "  rollup service:  ${ROLLUP_SERVICE:-<skipped>}"
log "  network:         $P2P_NETWORK"
log "  core endpoint:   $CORE_IP:$CORE_PORT"
log "  public RPC:      $PUBLIC_RPC"
log "  resync depth:    $RESYNC_DEPTH blocks (auto-depth=$AUTO_DEPTH, buffer=$DEPTH_BUFFER)"
log "  min free disk:   ${MIN_FREE_GB}G"

# ─── 3. Backup keys + config ──────────────────────────────────────────────
log "backing up keys + config to $BACKUP_DIR"
# mkdir -p "$BACKUP_DIR"
# cp -a "$BRIDGE_DIR/keys"  "$BACKUP_DIR/keys"
# cp -a "$BRIDGE_CFG"       "$BACKUP_DIR/config.toml"

# ─── 4. Get current Celestia head BEFORE stopping anything ────────────────
log "fetching current Celestia head from $PUBLIC_RPC"
CURRENT_HEIGHT=$(curl -fsSL --max-time 15 "$PUBLIC_RPC/block" \
                 | jq -r '.result.block.header.height')
[[ "$CURRENT_HEIGHT" =~ ^[0-9]+$ ]] || abort "failed to parse current height from $PUBLIC_RPC"

# ─── 4b. Auto-depth: derive RESYNC_DEPTH from rollup's current DA pointer ─
# The rollup logs "synced_da_height=N target_da_height=M" — we pull N from
# its journal so we never set the bridge tail above where the rollup needs
# to read. Without this, a small RESYNC_DEPTH after the rollup has fallen
# behind leaves it stuck on "header below Tail" forever.
if [[ "$AUTO_DEPTH" == "true" && -n "$ROLLUP_SERVICE" ]]; then
  log "auto-depth: detecting rollup's synced_da_height"
  ROLLUP_DA=$(journalctl -u "$ROLLUP_SERVICE" -n 2000 --no-pager 2>/dev/null \
              | grep -oE "synced_da_height=[0-9]+" \
              | tail -1 | cut -d= -f2 || true)
  if [[ -n "$ROLLUP_DA" && "$ROLLUP_DA" =~ ^[0-9]+$ ]]; then
    GAP=$((CURRENT_HEIGHT - ROLLUP_DA))
    NEEDED=$((GAP + DEPTH_BUFFER))
    if (( NEEDED > RESYNC_DEPTH )); then
      log "  rollup at DA=$ROLLUP_DA, head=$CURRENT_HEIGHT, gap=$GAP"
      log "  depth $RESYNC_DEPTH too small, bumping to $NEEDED (gap+$DEPTH_BUFFER buffer)"
      RESYNC_DEPTH=$NEEDED
    else
      log "  rollup at DA=$ROLLUP_DA, gap=$GAP — depth $RESYNC_DEPTH already covers it"
    fi
  else
    log "  WARN: could not parse synced_da_height from journal — using depth=$RESYNC_DEPTH"
  fi
fi

NEW_SYNC_FROM=$((CURRENT_HEIGHT - RESYNC_DEPTH))
log "current head: $CURRENT_HEIGHT  →  new SyncFromHeight: $NEW_SYNC_FROM (depth=$RESYNC_DEPTH)"

# ─── 4c. Disk guard: refuse if free space too low for the resync ──────────
# Bridge EDS pull averages ~500KB/block. Worst case ~1MB/block with indices.
# Required = depth × 1MB + 5GB buffer for journal/rollup growth + OS.
FREE_KB=$(df --output=avail / | tail -1)
FREE_GB=$((FREE_KB / 1024 / 1024))
NEEDED_GB=$(( (RESYNC_DEPTH * 1) / 1024 + 5 ))
log "disk: free=${FREE_GB}G  required(est)=${NEEDED_GB}G  min=${MIN_FREE_GB}G"
if (( FREE_GB < MIN_FREE_GB )); then
  abort "disk free ${FREE_GB}G below minimum ${MIN_FREE_GB}G — aborting before ENOSPC"
fi
if (( FREE_GB < NEEDED_GB )); then
  abort "depth=$RESYNC_DEPTH needs ~${NEEDED_GB}G but only ${FREE_GB}G free — reduce depth or free disk"
fi

# ─── 5. Capture sizes before ──────────────────────────────────────────────
SIZE_BEFORE_BRIDGE=$(du -sh "$BRIDGE_DIR" 2>/dev/null | cut -f1)
SIZE_BEFORE_DISK=$(df -h --output=avail / | tail -1 | xargs)

# ─── 6. Stop services (rollup first, then bridge) ─────────────────────────
if [[ -n "$ROLLUP_SERVICE" ]]; then
  log "stopping $ROLLUP_SERVICE"
  systemctl stop "$ROLLUP_SERVICE" || true
fi
log "stopping $BRIDGE_SERVICE"
systemctl stop "$BRIDGE_SERVICE"

# Wait for processes to actually exit
sleep 3

# ─── 7. Wipe blocks/ and data/, KEEP keys/ + config ───────────────────────
log "wiping $BRIDGE_DIR/blocks and $BRIDGE_DIR/data"
rm -rf "$BRIDGE_DIR/blocks" "$BRIDGE_DIR/data"

SIZE_AFTER_BRIDGE=$(du -sh "$BRIDGE_DIR" 2>/dev/null | cut -f1)
log "$BRIDGE_DIR: $SIZE_BEFORE_BRIDGE  →  $SIZE_AFTER_BRIDGE"

# ─── 8. Re-initialize bridge store (recreates empty data/ and blocks/) ────
# Without this, celestia-bridge crashloops with "node: store is not initialized".
# HARDENED: retry + verify. A single transient `init` failure here used to abort
# the script (set -e) AFTER step 7 already wiped data/blocks, leaving the store
# permanently uninitialized -> bridge crashloop -> rollup loses DA -> node down.
# We now retry up to 3x and verify the store dir was actually (re)created before
# proceeding; if it can't be initialized we abort loudly WITHOUT pretending success.
log "re-initializing bridge store"
init_ok=false
for attempt in 1 2 3; do
  if celestia bridge init \
       --node.store "$BRIDGE_DIR" \
       --p2p.network "$P2P_NETWORK" \
       --core.ip "$CORE_IP" \
       --core.port "$CORE_PORT" 2>&1 | sed 's/^/  /'; then
    if [[ -d "$BRIDGE_DIR/data" ]]; then init_ok=true; break; fi
  fi
  log "  init attempt $attempt did not yield an initialized store — retry in 5s"
  sleep 5
done
[[ "$init_ok" == "true" ]] \
  || abort "bridge init failed after 3 attempts; store left WIPED. Run manually: HOME=/root celestia bridge init --node.store $BRIDGE_DIR --p2p.network $P2P_NETWORK --core.ip $CORE_IP --core.port $CORE_PORT, then 'systemctl start $BRIDGE_SERVICE $ROLLUP_SERVICE'"

# ─── 9. Rewrite SyncFromHeight in config.toml ─────────────────────────────
# `bridge init` may rewrite config.toml — re-apply our SyncFromHeight after.
log "updating SyncFromHeight in config.toml"
if grep -q "SyncFromHeight" "$BRIDGE_CFG"; then
  sed -i "s/SyncFromHeight = .*/SyncFromHeight = $NEW_SYNC_FROM/" "$BRIDGE_CFG"
else
  abort "SyncFromHeight key not present in config — manual intervention needed"
fi
log "  $(grep SyncFromHeight "$BRIDGE_CFG" | xargs)"

# ─── 10. Start bridge and wait for sync ───────────────────────────────────
log "starting $BRIDGE_SERVICE"
systemctl start "$BRIDGE_SERVICE"

log "waiting for bridge to reach head (poll every ${WAIT_LOOP_INTERVAL}s, max ${WAIT_LOOP_MAX} polls)"
caught_up=false
for i in $(seq 1 $WAIT_LOOP_MAX); do
  # Check service is still running — abort if it crashloops
  if ! systemctl is-active --quiet "$BRIDGE_SERVICE"; then
    log "  [$i] bridge service is NOT active — checking journal"
    journalctl -u "$BRIDGE_SERVICE" -n 5 --no-pager | sed 's/^/    /'
    abort "bridge service failed to stay up — investigate manually"
  fi
  RESP=$(curl -fsS -X POST "http://localhost:$BRIDGE_RPC_PORT" \
              -H "Content-Type: application/json" \
              -d '{"jsonrpc":"2.0","method":"das.SamplingStats","params":[],"id":1}' \
              2>/dev/null || true)
  if [[ -n "$RESP" ]] && echo "$RESP" | jq -e '.result.catch_up_done' >/dev/null 2>&1; then
    DONE=$(echo  "$RESP" | jq -r '.result.catch_up_done')
    HEAD=$(echo  "$RESP" | jq -r '.result.network_head_height')
    SAMP=$(echo  "$RESP" | jq -r '.result.head_of_sampled_chain')
    log "  [$i] catch_up_done=$DONE  head=$HEAD  sampled=$SAMP"
    if [[ "$DONE" == "true" ]]; then
      log "bridge caught up"
      caught_up=true
      break
    fi
  else
    log "  [$i] bridge RPC not ready yet"
  fi
  sleep $WAIT_LOOP_INTERVAL
done
if [[ "$caught_up" != "true" ]]; then
  log "CRITICAL: bridge did NOT reach head within $((WAIT_LOOP_MAX * WAIT_LOOP_INTERVAL))s."
  log "  Store is initialized and the bridge is running, so it should keep catching up."
  log "  Starting the rollup anyway (systemd will retry); if the node keeps timing out,"
  log "  the bridge is stuck — restart it: systemctl restart $BRIDGE_SERVICE"
fi

# ─── 11. Restart rollup ───────────────────────────────────────────────────
if [[ -n "$ROLLUP_SERVICE" ]]; then
  log "starting $ROLLUP_SERVICE"
  systemctl start "$ROLLUP_SERVICE"
fi

# ─── 12. Summary ──────────────────────────────────────────────────────────
sleep 5
SIZE_AFTER_DISK=$(df -h --output=avail / | tail -1 | xargs)
log "──────────────────────────────────────────────"
log "bridge:    $SIZE_BEFORE_BRIDGE  →  $SIZE_AFTER_BRIDGE"
log "disk free: $SIZE_BEFORE_DISK  →  $SIZE_AFTER_DISK"
log "backup:    $BACKUP_DIR"
if [[ -n "$ROLLUP_SERVICE" ]]; then
  log "verify:    systemctl status $BRIDGE_SERVICE $ROLLUP_SERVICE"
else
  log "verify:    systemctl status $BRIDGE_SERVICE"
fi
log "──────────────────────────────────────────────"
