# Staging Runbook

Staging deployment of sticker-service on a **shared** Ubuntu VPS. Other
projects already live on this host, and the host-level nginx owns ports
80/443. This stack therefore exposes nothing to the internet directly:

```
host nginx :80/:443  (TLS terminator, added later — needs working DNS)
    ↓
127.0.0.1:8015       (compose nginx, loopback only)
    ↓
backend (Gunicorn) :8000  →  postgres / redis  (internal compose network)
```

PostgreSQL, Redis and the Gunicorn backend stay on the internal compose
network. The compose nginx publishes **only** `127.0.0.1:8015:80`.

Until the `STAGING_DOMAIN` DNS record resolves and the host nginx server
block is installed, staging is reachable **only from the VPS itself** via
`http://127.0.0.1:8015`.

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
curl -fsS http://127.0.0.1:8015/health/     # via compose nginx, on the VPS
curl -fsS https://$STAGING_DOMAIN/health/   # externally, once DNS + host nginx + TLS are set up
```

## TLS / public access (next step, not yet configured)

The compose nginx speaks plain HTTP on loopback only. Once the
`STAGING_DOMAIN` DNS record resolves, add a server block to the **host**
nginx proxying to `http://127.0.0.1:8015` and issue a certificate with the
host's certbot (same pattern as the existing sites on this VPS). The host
nginx then terminates TLS and forwards `X-Forwarded-Proto`/`Host`;
`config/settings/staging.py` already trusts those headers
(`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`), so Django requires no
changes. Do not create the server block or run certbot before DNS works.

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

A sha that is already in the local checkout is deployed without any
`git fetch`, so a rollback does not depend on GitHub being reachable.

After a successful rollback, return the checkout to main:
`git checkout main`.

### Flaky `git fetch` from the VPS

`github.com` resolves to several anycast IPs and at least one of them
(140.82.121.4) is unreachable from the staging VPS, so a fetch fails on a bad
DNS answer with `Failed to connect to github.com port 443`. Nothing is changed
on staging when this happens (the failure is before build). Every fetch in
`resolve-deploy-ref.sh` is retried (`DEPLOY_FETCH_ATTEMPTS`, default 3, with a
`DEPLOY_FETCH_RETRY_DELAY`-second pause, default 15); each attempt re-resolves
DNS. If all attempts fail, rerun the GitHub Actions job or run
`DEPLOY_REF=dev ./deploy/scripts/deploy.sh` on the server again.

Data restore (only if a migration caused damage):

```bash
gunzip -c backups/staging-<db>-<timestamp>.sql.gz | \
  docker compose --env-file .env.staging -f docker-compose.staging.yml \
    exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

## Generation worker (background generation, 2026-09-20)

The `worker` compose service runs ONE RQ worker (`manage.py generation_worker
--name sticker-generation-1`, queue `generation`, Redis logical DB 1) on the
same image as `backend`. While `GENERATION_WORKER_ENABLED=false` (the default)
the console still generates inline and the worker just idles; with the flag
`true` the console enqueues the job id and the worker runs the provider call.
Job state always lives in the database — Redis holds only job ids, so no Redis
persistence is configured or needed.

Guarantees and knobs (docker-compose.staging.yml):
- exactly one instance — never `--scale worker=2`, never run `generation_worker`
  by hand on the host or in a second container while the service runs (RQ
  refuses a second worker with the same name; the DB claim is the real gate);
- `mem_limit`/`memswap_limit` 512 MiB (shared VPS), `restart: unless-stopped`;
- `stop_grace_period: 600s` — on redeploy RQ finishes the in-flight provider
  call (≤ 540 s) before exiting; `deploy.sh` also waits for an idle worker
  before `up -d` and fails the deploy if the worker does not become healthy;
- healthcheck `manage.py worker_health --max-age 150` (heartbeat-based; idle
  heartbeat every 105 s with `--worker-ttl 120`, every 30 s while busy).

```bash
$COMPOSE ps worker                                                   # State / Health
$COMPOSE exec -T worker python manage.py worker_health --max-age 150 # ok queued=N <name> state=idle|busy heartbeat_age=..s
$COMPOSE logs --since 1h worker                                      # job start / done / fail lines
$COMPOSE exec -T redis redis-cli -n 1 llen rq:queue:generation       # queued (not yet claimed) job ids
$COMPOSE up -d --force-recreate --no-deps worker                     # restart the worker (waits ≤ 600 s for an in-flight job)
```

Failure modes:
- worker OOM-killed / crashed mid-job → container restarts; the job stays
  RUNNING in the DB and becomes `timeout_ambiguous` through the stale guard
  (≥ 15 min), exactly like a hung provider today. Never re-run it by hand —
  the operator decides in the console (retry / regenerate).
- Redis restarted → queued-but-unclaimed ids are gone; the job stays PENDING
  and the console shows it waiting for the worker; the operator re-enqueues or
  removes it from the queue. Nothing is charged.
- `worker_health` red while `$COMPOSE ps` says Up → Redis unreachable from the
  worker or the worker process wedged: check `logs worker`, then recreate it.

Enabling the flag on staging: set `GENERATION_WORKER_ENABLED=true` in
`.env.staging`, then `$COMPOSE up -d --force-recreate --no-deps backend`
(the worker reads the queue regardless of the flag). Roll back the same way
with `false` — in-flight jobs finish on the worker, new ones run inline.

## Backups

- `./deploy/scripts/backup.sh` → `./backups/staging-<db>-<UTC timestamp>.sql.gz`
- `backups/` is git-ignored; copies off-server are the operator's job (out of scope here).

## Telegram outbound transport (DRF-1870)

The staging VPS cannot reach `api.telegram.org` directly (TCP timeout to
149.154.166.110:443, IPv6 unreachable — measured from the backend container
on 2026-09-15). All Telegram outbound (JSON calls, `sendPhoto` multipart,
file downloads) goes through `apps/telegram_bot/client.py`, which supports
three modes via `.env.staging` (never hardcode credentials in code):

- **Direct mode (default):** all `TELEGRAM_*` transport vars empty →
  `https://api.telegram.org`. Production-compatible.
- **Relay/base mode:** `TELEGRAM_API_ORIGIN=https://<bot-api-relay>`
  (and optionally `TELEGRAM_FILE_ORIGIN` if file downloads use a different
  host; defaults to the API origin).
- **Proxy mode:** `TELEGRAM_PROXY_URL=http://user:password@proxy:3128` —
  applied to every Telegram call, upload and download. TLS verification
  stays on; the proxy only tunnels CONNECT.

## Outbound proxy pool (Telegram + OpenAI)

Geo-blocked upstreams (Telegram: TCP unreachable; OpenAI:
`403 unsupported_country_region_territory`) share one application-level
proxy pool (`apps/core/outbound_proxy.py`). Only the Telegram and OpenAI
clients use it — YooKassa, health checks and all other traffic stay direct.
Nothing on the host (routing, firewall, global `HTTPS_PROXY`) is changed.

```env
OUTBOUND_PROXY_ENABLED=true
OUTBOUND_PROXY_URLS_JSON=["http://user:password@proxy-a:3128","http://user:password@proxy-b:3128"]
OUTBOUND_PROXY_COOLDOWN_SECONDS=60
```

- JSON list (not comma-separated) so credentials may contain any characters.
- Selection is deterministic round-robin with per-service health: a
  transport failure (connect/timeout/reset, proxy auth) cools the proxy
  down for that service only; upstream API answers (Telegram 400/401,
  OpenAI 400/401/429) never rotate the proxy. OpenAI geo 403 blocks the
  proxy for OpenAI only. Cooldown recovers lazily on the next `select()`.
- Pool state is **per process** (each Gunicorn worker has its own); there is
  no shared circuit state across workers (MVP, by design).
- Precedence for Telegram: `TELEGRAM_PROXY_URL` (legacy single proxy) →
  pool → direct. For OpenAI: pool → direct.
- OpenAI failover is cost-safe: connect-phase failures and definitive geo
  rejections retry via the next proxy, but an ambiguous post-submit failure
  (e.g. read timeout) fails closed — no blind duplicate billable generation.
- HTTP/HTTPS CONNECT proxies work out of the box (httpx). `socks5://` URLs
  additionally require `httpx[socks]` (socksio), which is intentionally not
  installed until a SOCKS5 proxy is actually used.

Health probe (no credentials in output):

```bash
docker compose --env-file .env.staging -f docker-compose.staging.yml run --rm backend \
  python manage.py check_outbound_proxies
```

Expected output: per proxy `telegram: PASS/FAIL` and `openai: PASS/FAIL/GEO_BLOCKED`;
exit code is non-zero unless at least one proxy passes each service.
The Telegram probe hits `https://api.telegram.org/` (404 = transport OK);
the OpenAI probe is the non-billable `models.list()`.

Safe verification after changing transport env (token never printed):

```bash
docker compose --env-file .env.staging -f docker-compose.staging.yml run --rm backend python - <<'PY'
import os
from apps.telegram_bot.client import TelegramBotClient
me = TelegramBotClient(os.environ["TELEGRAM_BOT_TOKEN"]).get_me()
print("getMe ok:", me.get("username"))
PY
```
