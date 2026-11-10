#!/bin/bash
# Restore DRIFT Postgres from a backup.
# Usage: ./restore-postgres.sh <backup-dir>
# Example: ./restore-postgres.sh central_hub/deploy/backups/2026-04-06_12-00

set -e
COMPOSE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
BACKUP_ROOT="$(dirname "$0")/backups"

if [ -z "$1" ]; then
    echo "Available backups:"
    ls -dt "$BACKUP_ROOT"/20* 2>/dev/null | head -10 | while read d; do
        echo "  $(basename $d)  ($(du -sh "$d" | cut -f1))"
    done
    echo ""
    echo "Usage: $0 <backup-dir-name>"
    echo "Example: $0 2026-04-06_12-00"
    exit 1
fi

BACKUP_DIR="$BACKUP_ROOT/$1"
DUMP="$BACKUP_DIR/postgres.sql.gz"

[ -f "$DUMP" ] || { echo "ERROR: $DUMP not found"; exit 1; }

echo "WARNING: This will REPLACE all DRIFT data (clusters, user clusters) with the backup from: $1"
read -p "Continue? [y/N] " confirm
[ "$confirm" = "y" ] || { echo "Aborted."; exit 0; }

cd "$COMPOSE_DIR"

echo "Restoring Postgres from $1..."
gunzip -c "$DUMP" | docker exec -i drift-postgres psql -U drift -d drift

echo "Done. Restart the backend to pick up restored data:"
echo "  docker compose restart backend"
