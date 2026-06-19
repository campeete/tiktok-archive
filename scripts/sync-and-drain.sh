#!/usr/bin/env bash
# sync-and-drain.sh — sync all due creators, then drain up to N jobs.
#
# Wire into cron:
#   0 * * * * /path/to/tiktok-archive/scripts/sync-and-drain.sh
#
# Or launchd, systemd timer, etc.

set -euo pipefail

# Resolve project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Activate venv
if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/venv/bin/activate"
else
  echo "venv not found at $PROJECT_ROOT/venv" >&2
  exit 1
fi

cd "$PROJECT_ROOT"

# How many jobs to drain in one run
MAX_JOBS="${MAX_JOBS:-50}"

# Log file
LOG_DIR="$PROJECT_ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/sync-and-drain.log"

# Timestamp every line in the log
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "==== sync-and-drain start ===="
log "Syncing due creators..."
tiktok-archive creator sync --all 2>&1 | tee -a "$LOG_FILE"

log "Draining up to $MAX_JOBS jobs..."
tiktok-archive worker --once --max-jobs "$MAX_JOBS" 2>&1 | tee -a "$LOG_FILE"

log "==== sync-and-drain done ===="
