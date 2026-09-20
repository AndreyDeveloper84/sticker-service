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

# DEPLOY_REF controls what gets deployed:
#   unset    -> latest origin/main
#   <branch> -> latest origin/<branch> (e.g. DEPLOY_REF=dev for staging)
#   <sha>    -> that exact commit (used for rollback; no pull is performed)
# Resolution lives in resolve-deploy-ref.sh and prints the deployed sha.
DEPLOY_REF="${DEPLOY_REF:-}" ./deploy/scripts/resolve-deploy-ref.sh

echo "[deploy] building images"
$COMPOSE build

echo "[deploy] backing up database before migrations"
./deploy/scripts/backup.sh

echo "[deploy] running migrations"
$COMPOSE run --rm backend python manage.py migrate --noinput

echo "[deploy] collecting static files"
$COMPOSE run --rm --no-deps backend python manage.py collectstatic --noinput

# Generation worker: `up -d` will recreate it with the new image. RQ handles
# SIGTERM as a warm shutdown (finishes the in-flight job, then exits) and
# the service has stop_grace_period 600s, so a paid provider call is never
# killed. Still, wait for the worker to be idle first (best-effort, ≤ 600 s):
# a deploy that starts on an idle worker never has to sit in that grace
# period. No worker container yet (first deploy) → nothing to wait for.
worker_cid="$($COMPOSE ps -q worker 2>/dev/null || true)"
if [ -n "$worker_cid" ]; then
    echo "[deploy] waiting for the generation worker to become idle (≤ 600 s)"
    for _ in $(seq 1 60); do
        if ! $COMPOSE exec -T worker python manage.py worker_health --max-age 150 2>/dev/null | grep -q "state=busy"; then
            break
        fi
        sleep 10
    done
fi

echo "[deploy] starting services"
$COMPOSE up -d --remove-orphans

# The nginx config is a bind-mounted template rendered at container start.
# `up -d` leaves a running nginx untouched (the service definition did not
# change), and after `git checkout` the mount still points at the OLD file
# inode, so template changes never reach the running container. Recreating
# nginx is cheap (~5 s) and deterministic, so always do it.
echo "[deploy] recreating nginx to re-render the config template"
$COMPOSE up -d --force-recreate --no-deps nginx

echo "[deploy] waiting for backend healthcheck"
backend_cid="$($COMPOSE ps -q backend)"
if [ -z "$backend_cid" ]; then
    echo "[deploy] ERROR: backend container not found" >&2
    exit 1
fi

# wait_healthy <service> <container id> <attempts>: block until the container's
# healthcheck reports healthy; unhealthy or timeout = failed deploy (exit 1).
wait_healthy() {
    local service="$1" cid="$2" attempts="$3" status
    for _ in $(seq 1 "$attempts"); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
        if [ "$status" = "healthy" ]; then
            echo "[deploy] ${service} is healthy"
            return 0
        fi
        if [ "$status" = "unhealthy" ]; then
            echo "[deploy] ERROR: ${service} reported unhealthy" >&2
            $COMPOSE logs --tail=50 "$service" >&2 || true
            exit 1
        fi
        sleep 5
    done
    echo "[deploy] ERROR: ${service} healthcheck timed out" >&2
    $COMPOSE logs --tail=50 "$service" >&2 || true
    exit 1
}

wait_healthy backend "$backend_cid" 30

echo "[deploy] verifying HTTP health through nginx (loopback)"
if ! curl -fsS --max-time 10 "http://127.0.0.1:8015/health/" > /dev/null; then
    echo "[deploy] ERROR: /health/ through nginx failed" >&2
    exit 1
fi
echo "[deploy] OK: /health/ responds on 127.0.0.1:8015"

# The generation worker is part of the release: a deploy whose worker never
# comes up must be red, otherwise (with GENERATION_WORKER_ENABLED=true) jobs
# would silently queue until the stale guard. The healthcheck is heartbeat
# based; start_period 30 s + interval 30 s → allow up to ~3 min.
echo "[deploy] waiting for generation worker healthcheck"
worker_cid="$($COMPOSE ps -q worker)"
if [ -z "$worker_cid" ]; then
    echo "[deploy] ERROR: worker container not found" >&2
    exit 1
fi
wait_healthy worker "$worker_cid" 36
exit 0
