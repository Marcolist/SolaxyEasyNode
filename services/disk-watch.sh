#!/usr/bin/env bash
#
# disk-watch.sh — runs every 30 min via systemd timer.
#
# HARDENED 2026-06-28. The disk hog is svm-rollup/data/archival-state-db, which
# grows unbounded because pruning is disabled in the binary. Primary defence is a
# weekly cron running run-prune.sh; THIS is the backstop:
#
#   free >= 50G        → nothing to do
#   40G <= free < 50G  → WARN only (weekly cron will handle it)
#   free < 40G         → trigger the archival prune (run-prune.sh) detached, at
#                        most once per 12h. The prune reclaims ~all archival space
#                        (stops node ~25min, swaps in a pruned db, auto-rolls back
#                        on failure). This replaced the old bridge-wipe, which freed
#                        almost nothing and crash-looped the node.
#
set -euo pipefail

PRUNE="/root/projects/archival-prune/run-prune.sh"
PRUNE_UNIT="archival-auto-prune"
LOG="/var/log/disk-watch.log"
LOCK="/var/run/disk-watch.lock"
COOLDOWN_STAMP="/var/run/disk-watch.last-prune"
COOLDOWN_SECS=43200        # 12 hours

ts()  { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf "%s %s\n" "$(ts)" "$*" | tee -a "$LOG"; }

# single-instance lock — exit silently if another tick is still running
exec 9>"$LOCK"
if ! flock -n 9; then log "skip: previous tick still holding $LOCK"; exit 0; fi

FREE_GB=$(df --output=avail / | tail -1 | awk '{print int($1/1024/1024)}')

if (( FREE_GB >= 50 )); then
  log "OK: ${FREE_GB}G free — no action"
  exit 0
fi
if (( FREE_GB >= 40 )); then
  log "WARN: ${FREE_GB}G free — weekly archival prune will handle it (no action)"
  exit 0
fi

# free < 40G — backstop: trigger the archival prune
if systemctl is-active --quiet "$PRUNE_UNIT"; then
  log "CRITICAL: ${FREE_GB}G free but archival prune ($PRUNE_UNIT) already running — waiting"
  exit 0
fi
now=$(date +%s); last=0
[[ -f "$COOLDOWN_STAMP" ]] && last=$(cat "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
if (( now - last < COOLDOWN_SECS )); then
  log "CRITICAL: ${FREE_GB}G free but archival prune in cooldown ($(( (COOLDOWN_SECS-(now-last))/60 ))min left)"
  exit 0
fi
[[ -x "$PRUNE" ]] || { log "CRITICAL: ${FREE_GB}G free but $PRUNE missing/not executable"; exit 1; }

log "CRITICAL: ${FREE_GB}G free — launching archival prune ($PRUNE) detached (~25min downtime)"
echo "$now" > "$COOLDOWN_STAMP"
# run as an independent transient unit so it survives this oneshot's exit
systemd-run --collect --unit="$PRUNE_UNIT" "$PRUNE" 2>&1 | tee -a "$LOG" || \
  log "WARN: systemd-run failed to launch $PRUNE"
