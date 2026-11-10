#!/bin/bash
# DRIFT Postgres backup.
# Run manually or add to cron: 0 */6 * * * /path/to/backup.sh
#
# Backups are stored in: central_hub/deploy/backups/YYYY-MM-DD_HH-MM/
# Keep last 7 days automatically.

set -e
BACKUP_ROOT="$(dirname "$0")/backups"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M)
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting DRIFT backup..."

# ── Postgres dump ─────────────────────────────────────────────────────────────
echo "Backing up Postgres (management clusters, kubeconfigs)..."
docker exec drift-postgres pg_dump -U drift drift \
    | gzip > "$BACKUP_DIR/postgres.sql.gz"
echo "  → $BACKUP_DIR/postgres.sql.gz ($(du -h "$BACKUP_DIR/postgres.sql.gz" | cut -f1))"

# ── Clean up old backups (keep last 28 = 7 days at 6h interval) ───────────────
echo "Cleaning up old backups (keeping last 28)..."
ls -dt "$BACKUP_ROOT"/20* 2>/dev/null | tail -n +29 | xargs rm -rf 2>/dev/null || true

echo "[$(date)] Backup complete: $BACKUP_DIR"
