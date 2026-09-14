#!/usr/bin/env bash
# Timestamped PostgreSQL backup for the staging stack.
# Credentials are read from the server-side .env.staging; nothing is hardcoded.
# Backups are written to ./backups/ which is git-ignored.
set -euo pipefail

cd "$(dirname "$0")/../.."

COMPOSE="docker compose --env-file .env.staging -f docker-compose.staging.yml"
BACKUP_DIR="${BACKUP_DIR:-./backups}"

if [ ! -f .env.staging ]; then
    echo "[backup] ERROR: .env.staging not found" >&2
    exit 1
fi

# shellcheck disable=SC1091
set -a
. ./.env.staging
set +a

: "${POSTGRES_DB:?POSTGRES_DB must be set in .env.staging}"
: "${POSTGRES_USER:?POSTGRES_USER must be set in .env.staging}"

db_cid="$($COMPOSE ps -q db 2>/dev/null || true)"
if [ -z "$db_cid" ] || [ "$(docker inspect --format '{{.State.Running}}' "$db_cid" 2>/dev/null || echo false)" != "true" ]; then
    echo "[backup] db container is not running; skipping backup (fresh install?)"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
outfile="$BACKUP_DIR/staging-${POSTGRES_DB}-${timestamp}.sql.gz"

echo "[backup] dumping ${POSTGRES_DB} -> ${outfile}"
$COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$outfile"

echo "[backup] done: ${outfile}"
