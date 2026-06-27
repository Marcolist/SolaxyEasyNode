#!/usr/bin/env bash
#
# disk-watch.sh — runs every 30 min via systemd timer.
#
# HARDENED 2026-06-27. The bridge wipe used to fire at <50G and repeatedly
# recycled the celestia-bridge — which (a) frees almost nothing (the bridge is
# only a few GB; the real disk hog is svm-rollup/data/archival-state-db, which
# grows unbounded because pruning is disabled in the binary) and (b) destabilised
# the DA layer, crash-looping the rollup node. New policy:
#
#   free >= 50G        → nothing to do
#   20G <= free < 50G  → WARN only. NO wipe. Real fix is reclaiming archival-state-db.
#   free < 20G         → emergency hardened bridge recycle, but at most once / 6h
#                        (cooldown stops the 30-min wipe→resync→crash loop).
#
# The wipe is now only an ENOSPC backstop; it is NOT the disk-management strategy.
#
set -euo pipefail

CLEANUP="/root/SolaxyEasyNode/services/bridge-cleanup.sh"
LOG="/var/log/disk-watch.log"
LOCK="/var/run/disk-watch.lock"
COOLDOWN_STAMP="/var/run/disk-watch.last-wipe"
COOLDOWN_SECS=21600        # 6 hours

ts()  { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf "%s %s\n" "$(ts)" "$*" | tee -a "$LOG"; }

# single-instance lock — exit silently if another tick is still cleaning
exec 9>"$LOCK"
if ! flock -n 9; then
  log "skip: previous tick still holding $LOCK"
  exit 0
fi

FREE_GB=$(df --output=avail / | tail -1 | awk '{print int($1/1024/1024)}')

if (( FREE_GB >= 50 )); then
  log "OK: ${FREE_GB}G free — no action"
  exit 0
fi

if (( FREE_GB >= 20 )); then
  log "WARN: ${FREE_GB}G free — NOT wiping the bridge (low-value + risky). Reclaim archival-state-db (du -sh /root/svm-rollup/data/archival-state-db) — that is the real disk hog."
  exit 0
fi

# free < 20G — emergency. Wipe at most once per cooldown window.
now=$(date +%s)
last=0
[[ -f "$COOLDOWN_STAMP" ]] && last=$(cat "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
if (( now - last < COOLDOWN_SECS )); then
  log "CRITICAL: ${FREE_GB}G free but bridge recycle is in cooldown ($(( (COOLDOWN_SECS - (now - last)) / 60 ))min left). Archival reclaim is required NOW to avoid ENOSPC."
  exit 0
fi

log "CRITICAL: ${FREE_GB}G free — emergency hardened bridge recycle (buffer=1000, min-free=5G). NOTE: this is a backstop; the real fix is archival-state-db reclaim."
echo "$now" > "$COOLDOWN_STAMP"
DEPTH_BUFFER=1000 MIN_FREE_GB=5 "$CLEANUP" 2>&1 | tee -a "$LOG"
