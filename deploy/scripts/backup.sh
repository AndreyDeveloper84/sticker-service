#!/usr/bin/env bash
# Timestamped PostgreSQL backup for the staging stack.
# Credentials are read from the server-side .env.staging; nothing is hardcoded.
# Backups are written to ./backups/ which is git-ignored.
#
# Fail-closed: if the database already exists, a successful pg_dump is
# mandatory — this script exits non-zero otherwise (deploy.sh then aborts).
# The dump is skipped ONLY when the database provably does not exist yet or
# has never been initialized (no tables in the public schema).
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

echo "[backup] ensuring db container is up"
$COMPOSE up -d db

db_cid="$($COMPOSE ps -q db)"
echo "[backup] waiting for db healthcheck"
for _ in $(seq 1 30); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$db_cid")"
    [ "$status" = "healthy" ] && break
    sleep 2
done
if [ "$status" != "healthy" ]; then
    echo "[backup] ERROR: db did not become healthy; refusing to continue without backup" >&2
    exit 1
fi

db_exists="$($COMPOSE exec -T db psql -U "$POSTGRES_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${POSTGRES_DB}'" | tr -d '[:space:]')"
if [ "$db_exists" != "1" ]; then
    echo "[backup] database ${POSTGRES_DB} does not exist yet; nothing to back up (fresh install)"
    exit 0
fi

table_count="$($COMPOSE exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'" | tr -d '[:space:]')"
if [ "$table_count" = "0" ]; then
    echo "[backup] database ${POSTGRES_DB} has no tables yet; nothing to back up (fresh install)"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
outfile="$BACKUP_DIR/staging-${POSTGRES_DB}-${timestamp}.sql.gz"

echo "[backup] dumping ${POSTGRES_DB} -> ${outfile}"
if ! $COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip > "$outfile"; then
    rm -f "$outfile"
    echo "[backup] ERROR: pg_dump failed; aborting (fail-closed)" >&2
    exit 1
fi

if [ ! -s "$outfile" ]; then
    rm -f "$outfile"
    echo "[backup] ERROR: backup file is empty; aborting (fail-closed)" >&2
    exit 1
fi

echo "[backup] done: ${outfile}"
