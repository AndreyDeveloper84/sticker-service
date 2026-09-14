# Staging Runbook

Staging deployment of sticker-service on an Ubuntu VPS. Only nginx is exposed
to the internet; PostgreSQL, Redis and the Gunicorn backend stay on the
internal compose network.

## Files

| Path | Purpose |
|---|---|
| `docker-compose.staging.yml` | staging stack: db, redis, backend (Gunicorn), nginx |
| `config/settings/staging.py` | Django settings (inherits `production.py`, `DEBUG=False`) |
| `deploy/nginx/default.conf.template` | nginx reverse proxy (envsubst-rendered) |
| `deploy/scripts/deploy.sh` | fail-fast deploy: pull, build, backup, migrate, collectstatic, up, healthcheck |
| `deploy/scripts/backup.sh` | timestamped `pg_dump` into `./backups/` |
| `.env.staging.example` | env template — copy to `.env.staging` on the server |

## Prerequisites (server, one-time)

1. Ubuntu VPS with Docker Engine and the compose plugin (`docker compose version`).
2. Clone the repository, e.g. `/opt/sticker-service`.
3. `cp .env.staging.example .env.staging` and fill in real values.
   `.env.staging` is git-ignored and must never be committed.
4. DNS `A` record for `STAGING_DOMAIN` pointing at the VPS.

## First deploy

```bash
cd /opt/sticker-service
./deploy/scripts/deploy.sh
```

The script checks out `main`, builds images, runs `migrate --noinput`,
collects static files, starts the stack and waits for the backend
healthcheck. It exits non-zero if the healthcheck fails. Migration safety is
not guaranteed by Django itself — it relies on migration review plus the
mandatory pre-migration backup (see below).

`deploy.sh` always takes a database backup before migrations. The backup is
fail-closed: if the staging database already exists, a successful `pg_dump`
is mandatory and the deploy aborts otherwise. The dump is skipped only when
the database provably does not exist yet or has no tables (fresh install).

## Routine deploy

Same command — `./deploy/scripts/deploy.sh`. A timestamped database backup is
written to `./backups/` before every migration run.

## Useful commands

All compose commands need both files:

```bash
COMPOSE="docker compose --env-file .env.staging -f docker-compose.staging.yml"

$COMPOSE ps                     # service status
$COMPOSE logs -f backend        # Gunicorn/Django logs
$COMPOSE logs -f nginx          # nginx logs
$COMPOSE run --rm backend python manage.py migrate --noinput
$COMPOSE run --rm --no-deps backend python manage.py collectstatic --noinput
$COMPOSE run --rm backend python manage.py createsuperuser
./deploy/scripts/backup.sh      # manual DB backup
```

## Health verification

```bash
curl -fsS http://localhost/health/          # via nginx on the server
curl -fsS https://$STAGING_DOMAIN/health/   # externally, once TLS is set up
```

## TLS setup (next step, not yet configured)

Port 443 is already published and the nginx template documents where the SSL
block goes. Typical flow: install certbot on the host, issue a certificate
for `STAGING_DOMAIN`, mount `/etc/letsencrypt` into the nginx container,
uncomment/add the `listen 443 ssl;` server block in
`deploy/nginx/default.conf.template` and redirect :80 to :443.
`config/settings/staging.py` already trusts `X-Forwarded-Proto`/`Host`
(`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`), so Django requires no
changes.

## Media files

`/media/` is deliberately NOT served by nginx. Order photos and generated
assets contain customer data and must not be reachable by URL without
authentication. Delivery to customers goes through the bot APIs; operators
use the authenticated Django admin / Production Console.

## Rollback

`deploy.sh` supports a pinned ref via `DEPLOY_REF` (no checkout of main, no
pull), so code rollback deploys an exact previous commit:

```bash
DEPLOY_REF=<previous-good-sha> ./deploy/scripts/deploy.sh
```

After a successful rollback, return the checkout to main:
`git checkout main`.
2. Data restore (only if a migration caused damage):

```bash
gunzip -c backups/staging-<db>-<timestamp>.sql.gz | \
  docker compose --env-file .env.staging -f docker-compose.staging.yml \
    exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

## Backups

- `./deploy/scripts/backup.sh` → `./backups/staging-<db>-<UTC timestamp>.sql.gz`
- `backups/` is git-ignored; copies off-server are the operator's job (out of scope here).
