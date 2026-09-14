#!/usr/bin/env bash
# Staging deploy for sticker-service. Run on the staging server from the
# repository checkout. Fails fast; non-zero exit means the deploy did not
# complete and the previous containers may still be running.
set -euo pipefail

cd "$(dirname "$0")/../.."

COMPOSE="docker compose --env-file .env.staging -f docker-compose.staging.yml"

if [ ! -f .env.staging ]; then
    echo "[deploy] ERROR: .env.staging not found (copy .env.staging.example and fill it in)" >&2
    exit 1
fi

echo "[deploy] fetching latest main"
git fetch origin main
git checkout main
git pull --ff-only origin main

echo "[deploy] building images"
$COMPOSE build

echo "[deploy] backing up database before migrations"
./deploy/scripts/backup.sh

echo "[deploy] running migrations (non-destructive)"
$COMPOSE run --rm backend python manage.py migrate --noinput

echo "[deploy] collecting static files"
$COMPOSE run --rm --no-deps backend python manage.py collectstatic --noinput

echo "[deploy] starting services"
$COMPOSE up -d --remove-orphans

echo "[deploy] waiting for backend healthcheck"
backend_cid="$($COMPOSE ps -q backend)"
if [ -z "$backend_cid" ]; then
    echo "[deploy] ERROR: backend container not found" >&2
    exit 1
fi

for _ in $(seq 1 30); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$backend_cid")"
    if [ "$status" = "healthy" ]; then
        echo "[deploy] backend is healthy"
        echo "[deploy] verifying HTTP health through nginx"
        if curl -fsS --max-time 10 "http://localhost/health/" > /dev/null; then
            echo "[deploy] OK: /health/ responds through nginx"
            exit 0
        fi
        echo "[deploy] ERROR: /health/ through nginx failed" >&2
        exit 1
    fi
    if [ "$status" = "unhealthy" ]; then
        echo "[deploy] ERROR: backend reported unhealthy" >&2
        $COMPOSE logs --tail=50 backend >&2 || true
        exit 1
    fi
    sleep 5
done

echo "[deploy] ERROR: backend healthcheck timed out" >&2
$COMPOSE logs --tail=50 backend >&2 || true
exit 1
