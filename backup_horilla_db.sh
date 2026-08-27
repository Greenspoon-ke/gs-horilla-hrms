#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Horilla HRMS - PostgreSQL Backup Script
# ------------------------------------------------------------------------------
# What this does, and why:
#   1. Confirms Django is actually resolving to Postgres (not the SQLite
#      fallback) before backing anything up. Backing up the wrong DB silently
#      is worse than not backing up at all.
#   2. Confirms the DB container is healthy before running pg_dump against it.
#   3. Runs pg_dump in custom format (-F c) — compressed, and restorable
#      selectively/in parallel via pg_restore.
#   4. Copies the dump out of the container onto the host (a backup that only
#      exists inside the container it's protecting isn't a backup).
#   5. Verifies the dump is structurally valid using `pg_restore --list`,
#      which reads the dump's table of contents without touching any real
#      data. A dump that fails this is corrupt, even if pg_dump exited 0.
#   6. Enforces a retention window so backups don't grow unbounded on disk.
#   7. Leaves a clearly marked hook for off-site copy (S3, rsync, etc.) —
#      local-only backups don't protect against host/disk failure.
#   8. Logs every step with timestamps for auditability.
# ==============================================================================

# ---- Config ----
DB_CONTAINER="gs-horilla-hrms-db-1"
WEB_CONTAINER="gs-horilla-hrms-server-1"
DB_NAME="horilla"
DB_USER="postgres"
BACKUP_DIR="./backups"
LOG_DIR="./backups/logs"
RETENTION_DAYS=14
TIMESTAMP=$(date +%F_%H%M%S)
BACKUP_FILENAME="horilla_backup_${TIMESTAMP}.dump"
LOG_FILE="${LOG_DIR}/backup_${TIMESTAMP}.log"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

log() {
  echo "[$(date '+%F %T')] $1" | tee -a "$LOG_FILE"
}

fail() {
  log "ERROR: $1"
  exit 1
}

log "=== Starting Horilla DB backup ==="

# ---- Step 1: Confirm active DB engine ----
ACTIVE_ENGINE=$(docker exec "$WEB_CONTAINER" python manage.py shell -c \
  "from django.conf import settings; print(settings.DATABASES['default']['ENGINE'])" 2>/dev/null || echo "unknown")

log "Active DB engine reported by Django: $ACTIVE_ENGINE"

if [[ "$ACTIVE_ENGINE" != *postgresql* ]]; then
  fail "Django is not currently using PostgreSQL (engine: $ACTIVE_ENGINE). Aborting — check DATABASE_URL before proceeding."
fi

# ---- Step 2: Confirm DB container is healthy ----
CONTAINER_STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null || echo "unknown")
if [[ "$CONTAINER_STATUS" != "healthy" ]]; then
  fail "DB container '$DB_CONTAINER' is not healthy (status: $CONTAINER_STATUS)."
fi
log "DB container healthy."

# ---- Step 3: Run pg_dump inside the container ----
log "Running pg_dump..."
docker exec -t "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -F c -f "/tmp/${BACKUP_FILENAME}" \
  || fail "pg_dump failed."

# ---- Step 4: Copy dump out of the container onto the host ----
docker cp "${DB_CONTAINER}:/tmp/${BACKUP_FILENAME}" "${BACKUP_DIR}/${BACKUP_FILENAME}" \
  || fail "Failed to copy dump out of container."

docker exec "$DB_CONTAINER" rm -f "/tmp/${BACKUP_FILENAME}"

FILE_SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILENAME}" | cut -f1)
log "Backup copied to host: ${BACKUP_DIR}/${BACKUP_FILENAME} (${FILE_SIZE})"

# ---- Step 5: Verify the dump is structurally valid ----
log "Verifying dump integrity..."
docker cp "${BACKUP_DIR}/${BACKUP_FILENAME}" "${DB_CONTAINER}:/tmp/verify_${BACKUP_FILENAME}"
if docker exec "$DB_CONTAINER" pg_restore --list "/tmp/verify_${BACKUP_FILENAME}" > /dev/null 2>&1; then
  log "Verification passed: dump is structurally valid and restorable."
else
  docker exec "$DB_CONTAINER" rm -f "/tmp/verify_${BACKUP_FILENAME}"
  fail "Verification FAILED: dump did not pass pg_restore --list check. Investigate immediately — do not rely on this backup."
fi
docker exec "$DB_CONTAINER" rm -f "/tmp/verify_${BACKUP_FILENAME}"

# ---- Step 6: Enforce retention ----
log "Applying retention policy (${RETENTION_DAYS} days)..."
find "$BACKUP_DIR" -maxdepth 1 -name "horilla_backup_*.dump" -mtime +"$RETENTION_DAYS" -print -delete | tee -a "$LOG_FILE"

# ---- Step 7: Off-site copy (fill in once destination is decided) ----
# rsync -avz "${BACKUP_DIR}/${BACKUP_FILENAME}" user@remote-host:/path/to/backups/
# aws s3 cp "${BACKUP_DIR}/${BACKUP_FILENAME}" s3://your-bucket/horilla-backups/
log "NOTE: off-site copy step is not yet configured. Backup currently exists on this host only."

log "=== Backup completed successfully: ${BACKUP_FILENAME} ==="
exit 0