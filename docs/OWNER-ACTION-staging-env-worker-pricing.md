# OWNER ACTION — staging `.env.staging`: background worker flag + token pricing (prepared 2026-09-20, applied (see STATUS banner))
> **STATUS: COMPLETED / SUPERSEDED (2026-09-20).** Block 1 (GENERATION_WORKER_ENABLED=true) applied by Agent A at 10:07:29 UTC (owner GO); Block 2 (gpt-image-2 token rates + FX 84.1975 / 2026-09-19 / CBR) applied at 10:15:27 UTC (OWNER FX DECISION). Both survived the deploys of 69cfc79 and 43b3d2d (verified). Keep this file only as the rollback / re-apply procedure — the live state is in the staging runbook and docs/reports/PILOT-AUTOMATION-BASELINE-2026-09-20.md.


Prepared by Agent A after verifying dev `5c1ff38` on staging (worker container up, idle, healthy;
backend still inline). Nothing below has been applied. Two independent blocks — each can be
applied and rolled back on its own. All commands run on the VPS as root in
`/opt/sticker-service`; `$COMPOSE` = `docker compose --env-file .env.staging -f docker-compose.staging.yml`.

Rules: edit `.env.staging` only (never the compose file), never print its contents in a chat,
keep a copy before editing (`cp .env.staging .env.staging.bak-$(date -u +%Y%m%d-%H%M%S)`).

---

## Block 1 — Background generation worker (`GENERATION_WORKER_ENABLED`)

**Preconditions (all three, otherwise do not apply):**
1. PR #89 (C-2, FULL lazy chain) merged and deployed to staging;
2. D-1 (console: queued/generating card, hidden buttons, «Снять из очереди») merged and deployed;
3. Orchestrator (stickers-60) gives the explicit GO for staging.

**State today:** the variable is absent from `.env.staging` → settings default `false` →
`InlineExecutor` (console runs the provider call synchronously, as always). The `worker`
service already runs idle (`worker_health: ok … state=idle`), so enabling is an env change +
backend recreate only.

**Line to add** (append to the end of `.env.staging`):
```
GENERATION_WORKER_ENABLED=true
```
Optional, leave unset unless told otherwise (default 560 s, must stay > OpenAI read timeout in
the worker and < the 900 s stale-RUNNING guard):
```
#GENERATION_JOB_TIMEOUT_S=560
```

**Apply:**
```bash
$COMPOSE exec -T worker python manage.py worker_health --max-age 150      # must print "ok … state=idle" BEFORE
$COMPOSE up -d --force-recreate --no-deps backend                          # backend re-reads .env.staging
$COMPOSE up -d --force-recreate --no-deps nginx                            # nginx upstream re-resolve (cheap, ~5 s)
```
(the worker does not read the flag — it serves the queue regardless; no need to recreate it.)

**Verify (Agent A does this on signal):**
```bash
$COMPOSE exec -T backend python -c "import django; django.setup(); from apps.core.services.generation_queue import get_executor; print(type(get_executor()).__name__)"
# expected: RQExecutor
curl -fsS http://127.0.0.1:8015/health/                                    # {"status": "ok", ...}
$COMPOSE exec -T worker python manage.py worker_health --max-age 150       # ok … state=idle
```
Then exactly one owner-approved preview through the queue (billable): console → «Сгенерировать
превью» must return immediately with «в очереди / генерируется… job #N»; `worker_health` shows
`state=busy`; the job finishes with `output_metadata.worker.{picked_at, finished_at, duration_s, proxy}`.

**Rollback:** set `GENERATION_WORKER_ENABLED=false` (or delete the line) and recreate backend the
same way. In-flight jobs finish on the worker; new clicks run inline again. Nothing else to undo.

---

## Block 2 — Token pricing (`cost_source=ESTIMATED`, PR #87)

**State today:** none of these variables is set → every job's cost is `UNKNOWN` (the pilot
metrics say so explicitly; never 0). `PILOT_IMAGE_CALL_COST_RUB` (per-call tariff) is also unset.
Token pricing takes precedence over the per-call tariff when all three rates are present.

**Lines to add** (values from the orchestrator's owner-approved wave prompt; the FX block is the
owner's own numbers — there is no lookup in code):
```
# --- token pricing (owner GO 2026-09-20) ---
PILOT_TEXT_INPUT_USD_PER_1M=5.00
PILOT_IMAGE_INPUT_USD_PER_1M=8.00
PILOT_IMAGE_OUTPUT_USD_PER_1M=30.00
PILOT_TOKEN_PRICING_MODEL=gpt-image-2
PILOT_TOKEN_PRICING_VERSION=openai-pricing@2026-09-20
# FX: owner-provided; without PILOT_FX_USD_RUB the USD estimate is stored and RUB stays unknown
PILOT_FX_USD_RUB=<owner: e.g. 91.50>
PILOT_FX_DATE=<owner: YYYY-MM-DD>
PILOT_FX_SOURCE=<owner: e.g. cbr.ru>
```
Semantics to keep in mind (from `apps/core/services/generation_cost.py`):
- all three `*_USD_PER_1M` rates are required — a missing or invalid one fails closed (cost stays
  UNKNOWN, a warning is logged); an invalid/zero FX drops only the RUB part;
- rates are snapshotted into each job at creation — changing them later never re-prices old jobs;
- the cost is computed from the provider's actual `usage` and needs the `input_tokens_details`
  split (the gpt-image API reports `text_tokens` / `image_tokens`; the split is kept by the
  provider since #87 — not yet observed on a live staging job, confirm on the first priced one) —
  a job without the split stays UNKNOWN;
- decimal point, not comma; no currency signs; no quotes.

**Apply:**
```bash
$COMPOSE up -d --force-recreate --no-deps backend    # the snapshot is taken by the backend (web) at job creation
$COMPOSE up -d --force-recreate --no-deps worker     # same env_file; keeps both sides identical (waits ≤ 600 s if a job is in flight)
$COMPOSE up -d --force-recreate --no-deps nginx
```

**Verify (Agent A on signal, read-only):**
```bash
$COMPOSE exec -T backend python -c "import django; django.setup(); from apps.core.services.generation_cost import token_pricing; p = token_pricing(); print({k: p[k] for k in ('model','pricing_version','text_input_rate_usd_per_1m','image_input_rate_usd_per_1m','image_output_rate_usd_per_1m','fx_usd_rub','fx_date','fx_source')} if p else 'token pricing NOT active')"
$COMPOSE logs --since 5m backend | grep -c "generation_cost.token_pricing"   # expected 0 warnings
```
The next generation (any channel) then carries `input_metadata.cost.pricing` and, after the
provider answers, `cost_usd` / `cost_minor` with `cost_source=ESTIMATED`; «Метрики Pilot» shows
the estimate instead of «неизвестна».

**Rollback:** delete the block (or comment it out) and recreate backend + worker. Jobs already
priced keep their snapshot (by design); new jobs go back to UNKNOWN.

---

## Not part of this action (still open, separate owner decisions)
- `PILOT_IMAGE_CALL_COST_RUB` / `PILOT_OPERATOR_COST_PER_HOUR_RUB` — per-call tariff and operator
  hour rate (DRF-2111 C1/C2); token pricing supersedes the first, the second is still unset.
- PX6 proxy credential rotation (DRF-2041) — unchanged.
