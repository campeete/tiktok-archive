#!/usr/bin/env bash
# backup-and-cleanup.sh — snapshot the DB and prune old local backups.
#
# Wire into cron / launchd for nightly backups:
#   30 3 * * * /path/to/tiktok-archive/scripts/backup-and-cleanup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/venv/bin/activate"
else
  echo "venv not found at $PROJECT_ROOT/venv" >&2
  exit 1
fi

cd "$PROJECT_ROOT"

LOG_DIR="$PROJECT_ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/backup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# Retention: keep this many days of local backups.
RETENTION_DAYS="${RETENTION_DAYS:-30}"

log "==== backup start ===="
tiktok-archive backup-db 2>&1 | tee -a "$LOG_FILE"

BACKUP_DIR="$PROJECT_ROOT/data/db-backups"
if [ -d "$BACKUP_DIR" ]; then
  log "Pruning local backups older than $RETENTION_DAYS days..."
  find "$BACKUP_DIR" -name "tiktok-*.db" -type f -mtime +"$RETENTION_DAYS" -print -delete 2>&1 | tee -a "$LOG_FILE"
fi

log "==== backup done ===="
