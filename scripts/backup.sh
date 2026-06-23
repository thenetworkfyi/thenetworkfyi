#!/usr/bin/env bash
# Nightly Postgres backup. The DB is the only source of truth (job queue +
# memory graph), so this is the one piece of state worth backing up.
#
# Install on the host as a cron job, e.g.:
#   0 3 * * *  /home/USER/thenetwork/agent/scripts/backup.sh >> /var/log/thenetwork-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/thenetwork}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
DB_SERVICE="${DB_SERVICE:-db}"
DB_NAME="${POSTGRES_DB:-network_db}"
DB_USER="${POSTGRES_USER:-network}"

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/network_db-$stamp.sql.gz"

# Dump via the compose db container so no host psql client is required.
docker compose exec -T "$DB_SERVICE" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$out"
echo "Wrote $out"

# Prune old dumps.
find "$BACKUP_DIR" -name 'network_db-*.sql.gz' -mtime "+$RETAIN_DAYS" -delete
