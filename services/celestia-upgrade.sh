#!/usr/bin/env bash
#
# celestia-upgrade.sh — upgrade the celestia-node binary in place.
#
# Downloads a release from celestiaorg/celestia-node, verifies its checksum,
# stops the bridge (and an optional rollup service), swaps the binary, and
# brings everything back up. Keeps a timestamped backup of the old binary.
#
# Use when celestia mainnet has activated a header/consensus upgrade and
# your current binary refuses headers with "this node supports up to
# version N. Please upgrade to support new version".
#
# ─── Usage ────────────────────────────────────────────────────────────────
#   sudo ./celestia-upgrade.sh [--help]
#
# ─── Configuration (env vars or flags) ────────────────────────────────────
#   CELESTIA_BIN      Path to celestia binary    [default: /usr/local/bin/celestia]
#   TARGET_VERSION    Release tag (e.g. v0.30.2) [default: latest non-prerelease]
#   BRIDGE_SERVICE    systemd unit name          [default: celestia-bridge]
#   ROLLUP_SERVICE    optional rollup unit       [default: solaxy-node, "" to skip]
#   BACKUP_DIR        backup location            [default: <bin>.upgrade-backup-<timestamp>]
#   DOWNLOAD_DIR      working dir for tarball    [default: /tmp/celestia-upgrade-<pid>]
#   GITHUB_REPO       release source             [default: celestiaorg/celestia-node]
#   ASSET_PATTERN     asset name to pick         [default: celestia-node_Linux_x86_64.tar.gz]
#   SKIP_CHECKSUM     set to 1 to skip checksum  [default: unset]
#   WAIT_LOOP_MAX     bridge sync poll attempts  [default: 60 (= 5 min)]
#
# Each may also be passed as a long flag, e.g. --target-version v0.30.2.
#
# ─── Examples ─────────────────────────────────────────────────────────────
#   sudo ./celestia-upgrade.sh
#   sudo ./celestia-upgrade.sh --target-version v0.30.2
#   sudo ROLLUP_SERVICE="" ./celestia-upgrade.sh                 # bridge only
#   sudo ./celestia-upgrade.sh --asset-pattern celestia-node_Linux_arm64.tar.gz
#

set -euo pipefail

# ─── Defaults ─────────────────────────────────────────────────────────────
CELESTIA_BIN="${CELESTIA_BIN:-/usr/local/bin/celestia}"
TARGET_VERSION="${TARGET_VERSION:-}"               # auto-detected if empty
BRIDGE_SERVICE="${BRIDGE_SERVICE:-celestia-bridge}"
ROLLUP_SERVICE="${ROLLUP_SERVICE-solaxy-node}"     # set to "" to skip
BACKUP_DIR="${BACKUP_DIR:-}"                       # auto-named if empty
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/tmp/celestia-upgrade-$$}"
GITHUB_REPO="${GITHUB_REPO:-celestiaorg/celestia-node}"
ASSET_PATTERN="${ASSET_PATTERN:-celestia-node_Linux_x86_64.tar.gz}"
SKIP_CHECKSUM="${SKIP_CHECKSUM:-}"
WAIT_LOOP_MAX="${WAIT_LOOP_MAX:-60}"
WAIT_LOOP_INTERVAL=5

# ─── CLI parsing ──────────────────────────────────────────────────────────
usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --celestia-bin)     CELESTIA_BIN="$2"; shift 2 ;;
    --target-version|--version) TARGET_VERSION="$2"; shift 2 ;;
    --bridge-service)   BRIDGE_SERVICE="$2"; shift 2 ;;
    --rollup-service)   ROLLUP_SERVICE="$2"; shift 2 ;;
    --bridge-only)      ROLLUP_SERVICE=""; shift 1 ;;
    --backup-dir)       BACKUP_DIR="$2"; shift 2 ;;
    --download-dir)     DOWNLOAD_DIR="$2"; shift 2 ;;
    --github-repo)      GITHUB_REPO="$2"; shift 2 ;;
    --asset-pattern)    ASSET_PATTERN="$2"; shift 2 ;;
    --skip-checksum)    SKIP_CHECKSUM=1; shift 1 ;;
    -h|--help)          usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

log()   { printf "[%s] %s\n" "$(date '+%H:%M:%S')" "$*"; }
abort() { log "ERROR: $*"; exit 1; }
cleanup() { [[ -d "$DOWNLOAD_DIR" ]] && rm -rf "$DOWNLOAD_DIR"; }
trap cleanup EXIT

# ─── 1. Sanity ────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]]               || abort "must run as root (systemctl + binary swap)"
command -v jq    >/dev/null     || abort "jq not installed"
command -v curl  >/dev/null     || abort "curl not installed"
command -v tar   >/dev/null     || abort "tar not installed"
command -v sha256sum >/dev/null || abort "sha256sum not installed"
[[ -x "$CELESTIA_BIN" ]]        || abort "$CELESTIA_BIN not executable"
systemctl list-unit-files "$BRIDGE_SERVICE.service" >/dev/null 2>&1 \
  || abort "systemd unit $BRIDGE_SERVICE.service not found"
if [[ -n "$ROLLUP_SERVICE" ]] \
   && ! systemctl list-unit-files "$ROLLUP_SERVICE.service" >/dev/null 2>&1; then
  log "note: rollup unit $ROLLUP_SERVICE.service not found — skipping rollup stop/start"
  ROLLUP_SERVICE=""
fi

# ─── 2. Resolve target version ────────────────────────────────────────────
if [[ -z "$TARGET_VERSION" ]]; then
  log "resolving latest non-prerelease from $GITHUB_REPO"
  TARGET_VERSION=$(curl -fsSL "https://api.github.com/repos/$GITHUB_REPO/releases/latest" \
                   | jq -r '.tag_name')
  [[ -n "$TARGET_VERSION" && "$TARGET_VERSION" != "null" ]] \
    || abort "failed to resolve latest release tag"
fi

# ─── 3. Compare to currently installed version ────────────────────────────
CURRENT_VERSION=$("$CELESTIA_BIN" version 2>/dev/null \
                  | awk '/Semantic version/ {print $3}' || true)
log "current: ${CURRENT_VERSION:-unknown}    target: $TARGET_VERSION"

# Match accepts "v0.30.2" target == "v0.30.2" or "0.30.2" current.
if [[ -n "$CURRENT_VERSION" ]] \
   && [[ "${CURRENT_VERSION#v}" == "${TARGET_VERSION#v}" ]]; then
  log "already at $TARGET_VERSION — nothing to do"
  exit 0
fi

# ─── 4. Fetch release metadata + asset URL ────────────────────────────────
log "fetching release metadata for $TARGET_VERSION"
RELEASE_JSON=$(curl -fsSL "https://api.github.com/repos/$GITHUB_REPO/releases/tags/$TARGET_VERSION") \
  || abort "release $TARGET_VERSION not found in $GITHUB_REPO"

ASSET_URL=$(echo "$RELEASE_JSON" \
            | jq -r --arg name "$ASSET_PATTERN" '.assets[] | select(.name == $name) | .browser_download_url' \
            | head -1)
[[ -n "$ASSET_URL" ]] || abort "asset $ASSET_PATTERN not in release $TARGET_VERSION"

CHECKSUMS_URL=$(echo "$RELEASE_JSON" \
                | jq -r '.assets[] | select(.name | test("checksums.txt$")) | .browser_download_url' \
                | head -1)

# ─── 5. Download + verify ─────────────────────────────────────────────────
mkdir -p "$DOWNLOAD_DIR"
log "downloading $ASSET_PATTERN to $DOWNLOAD_DIR"
curl -fsSL "$ASSET_URL" -o "$DOWNLOAD_DIR/$ASSET_PATTERN"

if [[ -z "$SKIP_CHECKSUM" ]] && [[ -n "$CHECKSUMS_URL" ]]; then
  log "verifying sha256 against checksums.txt"
  curl -fsSL "$CHECKSUMS_URL" -o "$DOWNLOAD_DIR/checksums.txt"
  EXPECTED=$(grep " $ASSET_PATTERN\$" "$DOWNLOAD_DIR/checksums.txt" | awk '{print $1}')
  [[ -n "$EXPECTED" ]] || abort "no checksum entry for $ASSET_PATTERN"
  ACTUAL=$(sha256sum "$DOWNLOAD_DIR/$ASSET_PATTERN" | awk '{print $1}')
  [[ "$EXPECTED" == "$ACTUAL" ]] \
    || abort "checksum mismatch — expected $EXPECTED got $ACTUAL"
  log "checksum OK"
else
  log "skipping checksum verification"
fi

# ─── 6. Extract ───────────────────────────────────────────────────────────
log "extracting"
tar -xzf "$DOWNLOAD_DIR/$ASSET_PATTERN" -C "$DOWNLOAD_DIR"
NEW_BIN=$(find "$DOWNLOAD_DIR" -maxdepth 2 -type f -name celestia | head -1)
[[ -x "$NEW_BIN" ]] || abort "celestia binary not found in tarball"

# Verify the extracted binary actually reports the target version before
# doing anything destructive.
NEW_REPORTED=$("$NEW_BIN" version 2>/dev/null | awk '/Semantic version/ {print $3}')
[[ "${NEW_REPORTED#v}" == "${TARGET_VERSION#v}" ]] \
  || abort "extracted binary reports '$NEW_REPORTED' but target is '$TARGET_VERSION'"

# ─── 7. Stop services + backup + swap binary ──────────────────────────────
[[ -z "$BACKUP_DIR" ]] && BACKUP_DIR="$CELESTIA_BIN.upgrade-backup-$(date +%Y%m%d-%H%M%S)"
log "backing up current binary to $BACKUP_DIR"
cp -a "$CELESTIA_BIN" "$BACKUP_DIR"

if [[ -n "$ROLLUP_SERVICE" ]]; then
  log "stopping $ROLLUP_SERVICE"
  systemctl stop "$ROLLUP_SERVICE" || true
fi
log "stopping $BRIDGE_SERVICE"
systemctl stop "$BRIDGE_SERVICE" || true
sleep 2

log "installing new binary at $CELESTIA_BIN"
install -m 0755 "$NEW_BIN" "$CELESTIA_BIN"
INSTALLED=$("$CELESTIA_BIN" version 2>/dev/null | awk '/Semantic version/ {print $3}')
log "installed version: $INSTALLED"

# ─── 8. Reset failed state + start bridge ─────────────────────────────────
systemctl reset-failed "$BRIDGE_SERVICE" 2>/dev/null || true
log "starting $BRIDGE_SERVICE"
systemctl start "$BRIDGE_SERVICE"

log "waiting for bridge to become healthy (poll every ${WAIT_LOOP_INTERVAL}s, max ${WAIT_LOOP_MAX} polls)"
BRIDGE_RPC_PORT="${BRIDGE_RPC_PORT:-26658}"
HEALTHY=0
for i in $(seq 1 $WAIT_LOOP_MAX); do
  if ! systemctl is-active --quiet "$BRIDGE_SERVICE"; then
    log "  [$i] bridge service is NOT active — recent journal:"
    journalctl -u "$BRIDGE_SERVICE" -n 8 --no-pager | sed 's/^/    /'
    log "  abort: bridge crashed after upgrade. Restore previous binary with:"
    log "    cp $BACKUP_DIR $CELESTIA_BIN && systemctl start $BRIDGE_SERVICE"
    exit 1
  fi
  RESP=$(curl -fsS -X POST "http://localhost:$BRIDGE_RPC_PORT" \
              -H "Content-Type: application/json" \
              -d '{"jsonrpc":"2.0","method":"das.SamplingStats","params":[],"id":1}' \
              2>/dev/null || true)
  if [[ -n "$RESP" ]] && echo "$RESP" | jq -e '.result' >/dev/null 2>&1; then
    DONE=$(echo "$RESP" | jq -r '.result.catch_up_done')
    HEAD=$(echo "$RESP" | jq -r '.result.network_head_height')
    SAMP=$(echo "$RESP" | jq -r '.result.head_of_sampled_chain')
    log "  [$i] catch_up_done=$DONE  head=$HEAD  sampled=$SAMP"
    HEALTHY=1
    [[ "$DONE" == "true" ]] && break
  else
    log "  [$i] bridge RPC not ready yet"
  fi
  sleep $WAIT_LOOP_INTERVAL
done

[[ $HEALTHY -eq 1 ]] || log "warning: bridge never reported sampling stats — investigate"

# ─── 9. Start rollup ──────────────────────────────────────────────────────
if [[ -n "$ROLLUP_SERVICE" ]]; then
  systemctl reset-failed "$ROLLUP_SERVICE" 2>/dev/null || true
  log "starting $ROLLUP_SERVICE"
  systemctl start "$ROLLUP_SERVICE"
fi

# ─── 10. Summary ──────────────────────────────────────────────────────────
sleep 3
log "──────────────────────────────────────────────"
log "celestia:  ${CURRENT_VERSION:-unknown}  →  $INSTALLED"
log "backup:    $BACKUP_DIR"
log "verify:    $CELESTIA_BIN version"
if [[ -n "$ROLLUP_SERVICE" ]]; then
  log "verify:    systemctl status $BRIDGE_SERVICE $ROLLUP_SERVICE"
else
  log "verify:    systemctl status $BRIDGE_SERVICE"
fi
log "rollback:  cp $BACKUP_DIR $CELESTIA_BIN && systemctl restart $BRIDGE_SERVICE"
log "──────────────────────────────────────────────"
