# MAX-first M2 pre-flight — первая live-ячейка (MAX · `single-sticker` · 100 ₽)

Статус: **чек-лист** (docs-only). Ячейка M2 из матрицы
`docs/DRF-2054_pilot_e2e_live_runbook.md` (PR #36): MAX → YooKassa → preview →
approve/revision → FULL (1 slot) → QC → final delivery → `DELIVERED`.
Владелец прогона: оператор Pilot + Agent A (live/infra). Никакие значения
секретов в этот документ, в Linear, в чат и в логи не попадают — только
имена переменных и `present / absent / format-ok`.

Документ разделяет два уровня готовности:

| Уровень | Что доказывает | Что НЕ доказывает |
|---|---|---|
| **ENGINEERING READY** | транспорт, оплата (test-shop), console-действия, gate'ы и состояния проходят на staging на точном deploy SHA | что превью/FULL действительно персонализированы под фото клиента |
| **REAL PAID PERSONALIZED PILOT READY** | всё выше **плюс** канонический OpenAI `images.edit` даёт likeness по фото, QC PASS на реальном FULL-выводе, реальная оплата по решению владельца | — |

Пока §4 «Personalization blocker» не снят, максимум достижимого уровня — **ENGINEERING READY**.

---

## 1. Секреты и слоты в `/opt/sticker-service/.env.staging`

Проверяется только присутствие и форма. Ожидание на момент написания
(2026‑09‑17, staging `c733121`) — в колонке «факт».

| Переменная | Назначение | Требование для M2 | Факт 2026‑09‑17 |
|---|---|---|---|
| `OPENAI_API_KEY` | канонический ключ platform.openai.com с доступом к Images **edit** (reference‑based preview/FULL) | present, format‑ok (canonical `sk-…`), `models.list()` через ProxyPool → 200 | **present, но НЕ канонический** (Nodule‑формат → 401 `invalid_api_key` на api.openai.com) → блокер §4 |
| `OPENAI_IMAGE_MODEL` | модель для `images.edit` | present | present |
| `OPENAI_BASE_URL` | должен быть **absent** (canonical only, Nodule endpoint не подставлять) | absent | absent ✔ |
| `NODULE_IMAGE_API_KEY` | отдельный слот для Nodule (экспериментальный text‑to‑image, только после merge провайдера Agent C) | absent до merge; после — present только если куплен Image package | absent (ключ Nodule сейчас ошибочно лежит в `OPENAI_API_KEY`) |
| `IMAGE_PROVIDER` | выбор провайдера `openai|nodule` (появится с провайдером C) | для M2 — `openai` или absent (default openai); `nodule` для персонализированных операций = fail‑closed | absent (переменной ещё нет в коде) |
| `MAX_BOT_TOKEN` | официальный MAX‑бот (STG‑05B) | present; `GET /me` direct → 200 | present ✔ |
| `MAX_WEBHOOK_SECRET` | защита `/max/webhook/` | present | present ✔ |
| `YOOKASSA_SHOP_ID` | магазин YooKassa | present | present ✔ |
| `YOOKASSA_SECRET_KEY` | секрет магазина; `test_…` = тестовый магазин, `live_…` = боевой | present; **режим фиксируется в evidence** (test/live) | present, режим **test** |
| `YOOKASSA_RETURN_URL` | возврат после оплаты | present | present ✔ |
| `YOOKASSA_API_BASE` | override API base — должен быть absent (direct api.yookassa.ru) | absent | absent ✔ |
| `MAX_PAYMENT_PROVIDER_*` (3 шт.) | legacy MAX‑провайдер, не используется с YooKassa | могут быть пустыми | empty ✔ |
| `OUTBOUND_PROXY_ENABLED` / `OUTBOUND_PROXY_URLS_JSON` / `OUTBOUND_PROXY_COOLDOWN_SECONDS` | ProxyPool только для Telegram/OpenAI (DRF‑2041) | present; JSON‑list валиден; **credentials ротированы** до REAL PAID | present, JSON ok, **не ротированы** → для REAL PAID блокер (DRF‑2041) |
| `HTTP_PROXY` / `HTTPS_PROXY` (в контейнере) | глобального прокси быть не должно | absent | absent ✔ |
| `TELEGRAM_*` | не нужны для M2 (MAX path не ждёт Telegram relay) | — | — |

Webhook YooKassa в кабинете магазина должен указывать на
`https://stg.stickme.art/max/payment/webhook/` (проверяется в кабинете, не из кода).

---

## 2. Sanitized команды проверки с VPS

Все команды — на VPS из `/opt/sticker-service`. Ничего из вывода не содержит значений секретов.

```bash
cd /opt/sticker-service
C="docker compose --env-file .env.staging -f docker-compose.staging.yml"

# 2.1 deploy SHA == origin/dev и health
git rev-parse HEAD; git fetch -q origin dev && git rev-parse origin/dev
$C ps --format '{{.Service}} {{.Status}}'
curl -fsS --max-time 15 https://stg.stickme.art/health/; echo

# 2.2 миграции (0 unapplied, makemigrations чистый)
$C exec -T backend python manage.py showmigrations core | tail -6
$C exec -T backend python manage.py showmigrations | grep -c '\[ \]'
$C exec -T backend python manage.py makemigrations --check --dry-run

# 2.3 env: имена и present/absent/format-ok (значения не печатаются)
(set -a; . ./.env.staging >/dev/null 2>&1; set +a) && echo SHELL_SOURCE=PASS
$C config --quiet && echo COMPOSE_CONFIG=PASS
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env.staging \
  | sed -E 's/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/\1 \2/' \
  | awk '{printf "%-36s %s\n", $1, (length($2)>0 ? "present" : "empty")}'

# 2.4 backend видит переменные (присутствие + форма, не значение)
$C exec -T backend sh -c '
  for v in OPENAI_API_KEY MAX_BOT_TOKEN MAX_WEBHOOK_SECRET YOOKASSA_SHOP_ID YOOKASSA_SECRET_KEY YOOKASSA_RETURN_URL OUTBOUND_PROXY_ENABLED OUTBOUND_PROXY_URLS_JSON NODULE_IMAGE_API_KEY IMAGE_PROVIDER OPENAI_BASE_URL HTTPS_PROXY HTTP_PROXY; do
    eval val=\$$v; if [ -n "$val" ]; then echo "$v present"; else echo "$v absent"; fi
  done
  case "$OPENAI_API_KEY" in sk-*) echo "OPENAI_API_KEY format: canonical";; nodule_*) echo "OPENAI_API_KEY format: NODULE (wrong slot)";; *) echo "OPENAI_API_KEY format: other";; esac
  case "$YOOKASSA_SECRET_KEY" in test_*) echo "YOOKASSA mode: test";; live_*) echo "YOOKASSA mode: live";; *) echo "YOOKASSA mode: other";; esac'

# 2.5 egress: OpenAI через ProxyPool, MAX и YooKassa DIRECT
$C exec -T backend python manage.py check_outbound_proxies      # нужно: telegram PASS + openai PASS
$C exec -T backend python - <<'EOF'
import os, httpx
from apps.max_bot.client import MaxBotClient
me = MaxBotClient(os.environ["MAX_BOT_TOKEN"])._request("GET", "/me")
print("MAX /me direct:", "ok" if me.get("user_id") or me.get("username") else "unexpected")
r = httpx.get("https://api.yookassa.ru/v3/me", auth=(os.environ["YOOKASSA_SHOP_ID"], os.environ["YOOKASSA_SECRET_KEY"]), timeout=15)
j = r.json() if r.status_code == 200 else {}
print("YooKassa /v3/me direct:", r.status_code, "test=", j.get("test"), "status=", j.get("status"))
from apps.max_bot.payments_yookassa import YooKassaPaymentProvider
p = YooKassaPaymentProvider.from_env()
print("YooKassa proxy mounts:", [str(k) for k in getattr(p.http_client, "_mounts", {})] or "none (direct)")
EOF

# 2.6 каталог и внешние маршруты
$C exec -T backend python manage.py seed_live_test               # products=sticker-pack-9, single-sticker, style=comic
for p in max/webhook/ max/payment/webhook/; do printf "%s -> " $p; curl -s -o /dev/null -w '%{http_code}\n' https://stg.stickme.art/$p; done   # GET → 405
```

Критерии ENGINEERING READY по §2: HEAD == origin/dev, deploy run SUCCESS, health
`ok/db/redis`, 0 unapplied, env parse PASS, MAX `/me` ok, YooKassa `/v3/me` 200
без proxy mounts, `single-sticker` active с `price_minor=10000 RUB`, GET на
webhooks → 405. Для REAL PAID дополнительно: `check_outbound_proxies` → `openai PASS`
и `OPENAI_API_KEY format: canonical`.

---

## 3. Порядок шагов M2 и evidence

Клиентская часть — официальный MAX‑бот; операторская — `/admin/core/order/<id>/`
(Production Console). Каждое console‑действие — одна кнопка/URL, `POST`.
Ниже имена URL из `apps/core/production_console.py`, `qc_console.py`,
`final_delivery_console.py` (dev `c733121`).

| # | Шаг | Кто | Действие | Ожидаемое состояние | Evidence (записать) |
|---|---|---|---|---|---|
| 1 | Старт | клиент | `bot_started` в MAX | кнопки ровно `sticker-pack-9`, `single-sticker` | `ChannelIdentity` id (max) |
| 2 | Продукт + стиль | клиент | `single-sticker` → `comic` | Order `AWAITING_PHOTOS`; 9 кнопок эмоций | **order id** |
| 3 | Эмоция | клиент | `emotion:<code>` | `Order.selection.emotions` = 1 код | код эмоции |
| 4 | Фото | клиент | 2–3 фото (только владельца/согласного тестера) | `OrderPhoto` ×N | photo ids |
| 5 | Consent (PR #37) | клиент | «Фото загружены» → текст согласия → `consent:accept` | `Order.consent_accepted=true`, `consent_version`; до merge #37 шага нет — checkout сразу | consent_version |
| 6 | Checkout | клиент/бот | кнопка «Оплатить заказ» с URL YooKassa | `READY_FOR_CHECKOUT` → `AWAITING_PAYMENT`; `Payment(yookassa, 10000 RUB, PENDING)` | **payment id**, `external_payment_id` |
| 7 | Оплата | клиент | YooKassa checkout (тест‑карта в test‑режиме / реальная — только по явному решению владельца) | webhook `payment.succeeded` → `Payment.CONFIRMED`, Order `PAID`, ровно один CONFIRMED; повтор webhook идемпотентен | `confirmed_at`, `metadata.provider_webhook` present |
| 8 | PAID notice (PR #37) | бот | `notify_customer_paid` | клиент получил «Оплата получена. Готовим ваше превью.» ровно один раз | **message id** уведомления |
| 9 | Preview | оператор | `core_order_generate_preview` | `PREVIEW_GENERATING` → `INTERNAL_PREVIEW_REVIEW`; `GenerationJob(preview)` SUCCEEDED, `output_metadata.usage` present | **preview job id**, asset id |
| 10 | Approve + deliver preview | оператор | `core_order_approve_preview` (asset) → deliver | `PREVIEW_REVIEW`; клиент получил фото + «Как вам превью?» | message id превью |
| 11a | Клиент «Нравится» | клиент | callback approve | `customer_approved=true`, статус `PREVIEW_REVIEW` | — |
| 11b | (вариант) «Нужно исправить» | клиент | категория правки | `REVISION_REQUESTED`, `Revision` ×1; вторая правка → 409 | revision id |
| 11c | Generate Revision (DRF‑2066) | оператор | `core_order_generate_revision` | `REVISION_GENERATING` → `INTERNAL_PREVIEW_REVIEW`; `GenerationJob(revision)` SUCCEEDED → шаг 10 → 11a | **revision job id** |
| 12 | Start Full Production | оператор | `core_order_start_full_production` — **один slot за нажатие**, для single повторять до завершения (1 раз) | `PACK_GENERATING` → `QUALITY_CONTROL`; `GenerationJob(full)` ×1 SUCCEEDED, `input_metadata.source_preview_id` = одобренное превью; повторное нажатие после завершения — ошибка, новых jobs нет | **full job id(s)**, FINAL asset id |
| 12b | (при failed slot) | оператор | `core_order_retry_failed_production` / `core_order_force_retry_slot` | новый attempt того же slot_key | attempt №, job id |
| 13 | QC report | оператор | `core_order_qc_start` | `QcReport attempt=1`, `expected_count=1`, automated checks `dimensions` / `alpha_channel` / `file_size` (см. §5) | **QcReport id**, automated_checks |
| 14 | QC checklist | оператор | `core_order_qc_finalize` — все 6 критериев явно | `PASSED` → `READY_FOR_DELIVERY`; любой снятый → `FAILED` + reason codes, статус не меняется; `core_order_qc_retry <slot>` → `PACK_GENERATING` → шаг 12 → attempt=2 | verdict, reason_codes |
| 15 | Deliver final set | оператор | `core_order_deliver_final` | `DELIVERY_IN_PROGRESS` → клиент получил 1 файл (MAX `send_image`) + «набор готов» → `DELIVERED`; `FinalDelivery attempt=1`, slot `sent` с message_id | **FinalDelivery id**, per‑slot **message id** |
| 15b | (при обрыве) | оператор | `core_order_resume_final_delivery` — только явный Resume; после `DELIVERED` — отказ | нет повторных отправок (принятое окно: ≤1 дубль при падении между send и записью message_id) | — |
| 16 | Metrics (после merge #34) | оператор | `python manage.py pilot_metrics --since <D> --until <D> --json`; `ManualWorkLog` минуты | `orders_paid=1`, `yookassa/RUB (1, 10000)`, `delivery=1 DELIVERED` | snapshot JSON |

Шаблон evidence ячейки:

```
CELL: M2 | deploy: <sha> | yookassa_mode: test|live | operator: <name> | date: <YYYY-MM-DD>
identity=<id> order=<id> product=single-sticker emotions=[<code>] consent_version=<v> status=DELIVERED
payment=<id> yookassa RUB 10000 external=<id> confirmed_at=<ts> paid_notice_msg=<id>
preview_job=<id> revision_job=<id|-> full_jobs=[<id>] final_asset=<id>
qc=[attempt1: PASSED|FAILED reasons=[...] automated={dimensions,alpha_channel,file_size}]
delivery=[FinalDelivery <id>: attempt1 slot sent msg=<id>; summary msg=<id>]
gaps/anomalies: <...>
LEVEL: ENGINEERING READY | REAL PAID PERSONALIZED PILOT READY
VERDICT: PASS | FAIL(<stage>, <reason>)
```

Negative gates после зелёной ячейки (без новых оплат, на том же заказе):
console отклоняет Start full production до «Нравится», QC report при неполном
наборе и Deliver final set до QC PASS (G3/G5/G6 из `tests_e2e_pilot_gates.py`).

---

## 4. Personalization blocker

Факт на 2026‑09‑17: `OPENAI_API_KEY` на staging — ключ Nodule (продукт GPT Codex),
канонический `api.openai.com` отвечает 401. `OpenAIImageProvider._generate`
вызывает `images.edit` с reference‑фото — единственный механизм identity в системе.
Следствия:

- шаги 9, 11c, 12 (preview / revision / FULL) **не работают** — любая попытка
  завершится `GenerationJob FAILED` без биллинга (401);
- Nodule (`/v1/images/generations`, text‑to‑image) **не даёт identity** — `/v1/images/edits` → 404;
  даже с купленным Image package он не заменяет шаги 9–12 для персональных стикеров;
- **реальные деньги клиента не брать**, пока блокер не снят: заказ не будет доведён
  до превью, а возврат — ручная операция в YooKassa.

Снятие блокера — одно owner‑действие: канонический ключ platform.openai.com
(с доступом к Images edit) → в `.env.staging` как `OPENAI_API_KEY`; Nodule‑ключ
перенести в `NODULE_IMAGE_API_KEY` (или удалить). Затем: recreate backend →
`check_outbound_proxies` → `openai PASS` → ровно одна контролируемая попытка
preview на существующем оплаченном Order 8 (DRF‑1871), только потом M2.

Максимально возможный dry‑run до снятия блокера (уровень ENGINEERING READY, шаги 1–8):

| Вариант | Условие | Что доказывает |
|---|---|---|
| **A. Реальный YooKassa до PAID** | только по явному решению владельца, его собственная карта; `YOOKASSA_SECRET_KEY` = `live_…`; сумма 100 ₽ | consent → checkout → webhook → `Payment.CONFIRMED` / `PAID` → PAID notice в боевом режиме; далее заказ остаётся `PAID` до снятия блокера (возврат при необходимости — из кабинета YooKassa) |
| **B. Synthetic (default)** | тестовый магазин (`test_…`, как сейчас) и тест‑карта YooKassa | тот же путь без денег; уже доказан на Order 8 (Payment 7 CONFIRMED). Используется, если владелец не принял решения по A |

В обоих вариантах шаги 9–15 не выполняются; evidence ячейки помечается
`LEVEL: ENGINEERING READY (до PAID)`, `VERDICT: BLOCKED(preview, OPENAI_API_KEY not canonical)`.

---

## 5. Известный риск QC (формат FULL‑вывода)

`OpenAIImageProvider._generate` вызывает `images.edit` без `size` / `background`.
Автоматические проверки QC (DRF‑2052) требуют сторону **512 px** и **alpha‑канал**.
Реальный вывод, вероятно, 1024 px и/или непрозрачный → `QcReport.automated_checks`
даст `bad_dimensions` / `missing_alpha`, QC не сможет пройти `PASSED`, delivery
заблокирована (gate G6). Это ожидаемое поведение gate, не дефект QC.

Действие при первом реальном FULL (шаг 12–13):

1. зафиксировать факт: реальные `width×height`, `mode` (RGB/RGBA), размер файла,
   `automated_checks` из QcReport, `output_metadata` (model/usage) FULL‑job'а;
2. **не** менять провайдера/QC на staging руками и не править БД;
3. передать факт Agent D (QC) и Agent C (провайдер) — решение между параметрами
   провайдера (`size`, `background="transparent"`) и post‑processing перед QC
   принимается по этому evidence (кандидат на follow‑up DRF‑2052);
4. ячейка в отчёте — `FAIL(QC, <reason codes>)`, но транспорт/оплата/генерация
   засчитываются как evidence ENGINEERING READY.

---

## 6. Чек‑лист «GO» перед M2

ENGINEERING READY (можно запускать шаги 1–8, synthetic):
- [ ] staging HEAD == origin/dev, deploy run SUCCESS, health ok/db/redis
- [ ] 0 unapplied migrations, `makemigrations --check` чистый
- [ ] env parse PASS; `MAX_BOT_TOKEN`, `MAX_WEBHOOK_SECRET`, `YOOKASSA_*` present; `YOOKASSA_API_BASE`, `OPENAI_BASE_URL`, `HTTP(S)_PROXY` absent
- [ ] MAX `/me` direct ok; YooKassa `/v3/me` direct 200; proxy mounts none
- [ ] `seed_live_test` выполнен; `single-sticker` active, 10000 RUB / 100 XTR
- [ ] PR #37 (consent + PAID notice) в dev и на staging — иначе шаги 5 и 8 отсутствуют
- [ ] оператор со staff‑доступом к консоли; тестер с MAX‑аккаунтом и согласием на фото

REAL PAID PERSONALIZED PILOT READY (дополнительно к списку выше):
- [ ] `OPENAI_API_KEY` канонический; `check_outbound_proxies` → `openai PASS`
- [ ] одна контролируемая preview‑попытка на Order 8 успешна (DRF‑1871), asset получен
- [ ] PX6 proxy credentials ротированы (DRF‑2041)
- [ ] решение владельца по варианту оплаты (A/B из §4) зафиксировано
- [ ] решение по формату FULL/QC (§5) принято или осознанно принят ожидаемый `FAIL(QC)`
