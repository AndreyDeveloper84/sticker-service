# Sticker Service — Pilot & Automation Baseline

Дата: 2026-09-20 (~09:30 UTC). Автор: оркестратор. Основание: код `origin/dev`, GitHub PR/CI, staging VPS (read-only), логи.
Linear в момент аудита недоступен (MCP CONNECT_TIMEOUT) — ссылки на DRF-ID даны по промпту владельца и предыдущим
сессиям; синхронизация Linear выполняется отдельным шагом после восстановления связи (см. §10/§11).

Baseline SHA: `origin/dev` = **5c1ff38** (после PR #88), `origin/main` = **7ecc77d** (DRF-1870; отстаёт от dev на 152 коммита —
main не используется как release-ветка пилота; пилот живёт на staging с dev). Миграции: `0001…0012_revision_category_clothes`,
линейно, `makemigrations --check` чисто на каждом PR. CI на dev: 1018+ тестов, 0 skipped (последние зелёные merge-run #85/#87/#88).

## 1. Executive summary

**Работает и доказано живьём (staging):** полный цикл заказа в обоих каналах — MAX (YooKassa test-shop, RUB) и Telegram
(Stars, XTR): меню → продукт → стиль → фото → (фразы) → контакт (необязателен) → карточка → согласие → оплата → превью →
одобрение/одна правка → FULL → QC → доставка. Последние живые ячейки: **Order 16** (MAX, single, правка + доставка PNG файлом,
DELIVERED 2026-09-20 06:51Z) и **Order 17** (Telegram, single, Stars 100 XTR → DELIVERED 07:46Z, 5,5 мин от оплаты до доставки).
Ранее: Orders 11 (M2 single), 12 (M1 pack-9), 13 (внешний клиент) DELIVERED 2026-09-18 на более старом коде.

**Реализовано, но без live-подтверждения после последних изменений:** генерация после рефакторинга executor (#85, C-1) —
ни одного живого вызова на коде ≥ 972051f; фоновая генерация (worker) — код и контейнер задеплоены, **флаг выключен**,
консольная часть (D-1) и FULL-цепочка (C-2, PR #89) ещё не влиты; стоимость по токенам (#87) — задеплоена, **ставки/курс не
заданы** → всё «неизвестна»; продукт «9 стикеров с надписями» (800 ₽/736 XTR) — **0 живых заказов**, только 8 unit-тестов.

**Блокирует контролируемый запуск (P0):** (1) финальная выдача как реально используемый стикер (Telegram — авто-набор через
Bot API; MAX — только инструкция «Стикеры в MAX» + Цифровой ID); (2) завершение и включение фоновой генерации (Order 14 —
два зависших вызова >300 с, деньги UNKNOWN); (3) live-acceptance на текущем коде и captioned SKU до его продажи; (4) owner-решения:
REAL PAID (PX6 ротация, YooKassa live-shop), ставки/курс для экономики, лимиты бюджета, media retention.

**После запуска (P2):** напоминания о превью, повторный заказ, алерты бюджета, QC-автоматика, аватары.

## 2. Current product contract

Источник истины: `apps/core/management/commands/seed_live_test.py` (идемпотентный seed) → `Product.config`; бот берёт цены из
`Product.config` (bot_menu.prices_text), checkout MAX = `price_minor` (RUB), Telegram = `price_stars` (XTR). XTR — независимая
канальная цена, не конвертация.

| SKU | Telegram | MAX | Status | Evidence |
|---|---|---|---|---|
| 1 персональный стикер (`single-sticker`) | 100 XTR | 100 ₽ | VERIFIED | staging DB 2026-09-20; Order 17 (TG, paid 100 XTR, delivered); Orders 11/13/16 (MAX, 100 ₽) |
| 9 стикеров без надписей (`sticker-pack-9`) | 460 XTR | 500 ₽ | VERIFIED (MAX) / IMPLEMENTED_NOT_LIVE_VERIFIED (TG) | Order 12 (MAX, 500 ₽, delivered 09-18); TG-инвойс 460 — только тесты |
| 9 стикеров с надписями (`sticker-pack-9-custom`) | 736 XTR | 800 ₽ | IMPLEMENTED_NOT_LIVE_VERIFIED | seed/DB верны; 0 заказов на staging; tests_full_generation_custom (8), tests_bot_menu_flow |
| Стили: 3D, Рисованные, Мемные, Вышивка, Помогите выбрать | — | — | VERIFIED | DB: 5 active, `comic` inactive; промпты «not a retouched photograph» (#79) live с 338164b; Orders 16/17 сгенерированы 3D |
| Аватары | — | — | не в Pilot | нет продукта/кода |

Документация/контракт: `01-ux/bot-mvp-contract.md` обновлён в #83/#84 (навигация, контакт необязателен). Противоречий кода
с контрактом не найдено. Требования к результату (PNG, одна сторона 512, альфа, ≤512 КБ, белая обводка) — в FULL_OUTPUT_REQUIREMENTS +
QC normalization (512 px) + автопроверки QC (alpha_channel, dimensions, file_size, mime_type, decodable, file_present, expected_count).

## 3. Implemented and verified

Статусы: VERIFIED / IMPLEMENTED_NOT_LIVE_VERIFIED / IN_PROGRESS / PLANNED / BLOCKED / UNKNOWN.

| Capability | Linear | PR / commit | Tests | Live evidence | Status |
|---|---|---|---|---|---|
| Bot main menu, 3 products, 5 styles, phrases, contact, card, consent (MAX+TG) | DRF-2050/2054, патч 4d9b244 | #66–#69 → fdf5af9 | tests_bot_menu_flow (21), tests_bot_navigation, matrix | Orders 14–17 созданы через новое меню | VERIFIED |
| Bot navigation hardening (re-prompt, «Продолжить заказ», stale buttons, /status, «Где я?», фото-до-эмоции hotfix) | DRF-2148 | #83 → ea0622c | tests_bot_navigation (19×2+3) | Order 17 доведён после деплоя (06:36Z) | VERIFIED |
| Контакт необязателен («Пропустить — свяжемся здесь») | DRF-2148 (доп.) | #84 → 860c7b4 | tests_bot_menu_flow/console_custom | без live-заказа с пропуском | IMPLEMENTED_NOT_LIVE_VERIFIED |
| Payments: YooKassa (MAX, test-shop) idempotent confirm, fee из income_amount | DRF-2069/2111 | #37, #71 | tests_payment, tests_yookassa (31) | Orders 14/16: CONFIRMED, fee 3,50 ₽ PROVIDER_CONFIRMED | VERIFIED (test-shop) |
| Payments: Telegram Stars invoice/pre_checkout/successful_payment | DRF-2057/2077 | #49/#50 | tests_payments (TG) | Order 17: 100 XTR CONFIRMED 07:40Z, charge_id сохранён | VERIFIED |
| Возврат Stars из консоли (refundStarPayment, at-most-once, event payment.refunded) | (в DRF-2160) | #86 → 6f89525 | tests_stars_refund (14) | не выполнялся (Order 17 ждёт нажатия) | IMPLEMENTED_NOT_LIVE_VERIFIED |
| Consent gate (fail-closed) оба канала | DRF-2069/2077 | #37, #49 | gates G9 | Orders 14–17 consent_accepted_at | VERIFIED |
| Preview generation (OpenAI images.edit, photos-first refs, SFW/FRAMING, transparent) | DRF-2051/2080/2089 | #31, #56, #62–#64 | tests_generation_service, prompt tests | Order 17 job 34 (38 с), Order 16 job 29 | VERIFIED (до #85) |
| Revision (одна на заказ, категории + «Сменить одежду», инструкция вместо кода категории) | DRF-2066, DRF-2160 | #38, #76, #80 → cce86e7 | tests_revision_prompt, tests_revision_clothes | Order 16 job 30 (37 с, старый промпт); «Сменить одежду» live не нажималась | VERIFIED / clothes IMPLEMENTED_NOT_LIVE_VERIFIED |
| FULL production (slots, retry/regenerate/force, guards) | DRF-2051 | #31, #39 | tests_full_generation (3 модуля) | Order 17 job 35 (60 с), Order 16 job 33, Order 12 (9 слотов) | VERIFIED (до #85) |
| Captioned slots (render_caption, phrase per slot) | DRF-2166 | #67 → fdf5af9 | tests_full_generation_custom (8) | 0 живых | IMPLEMENTED_NOT_LIVE_VERIFIED |
| QC: автопроверки + 7 human-критериев + normalization 512 + retry slots + Норма/Дефект | DRF-2052/2076/2079/2084 | #30, #47, #55, #58 | tests_qc*, qc_retry_console | Orders 12/16/17 QC PASS; Order 10 FAIL→retry | VERIFIED |
| Final delivery: Telegram sendDocument; MAX file attachment (alpha сохраняется) | DRF-2053, DRF-2163 | #35, #81 → ddfe2ce | tests_final_delivery* (128 с #81) | Order 16 MAX file (байт-в-байт, RGBA, проверено через MAX API); Order 17 TG document | VERIFIED (delivery) — но не «usable as sticker», см. §7 |
| Budget Guard (per-order 15, per-slot 3, day/month env; override с аудитом; PENDING учитывается) | DRF-2086 | #60/#61, #85 | tests_pilot_budget*, pending | отказ без платного вызова проверен live 09-18 | VERIFIED |
| Cost snapshot per job (immutable, UNKNOWN != 0, mode-маркер) | DRF-2111 | #70 → e4bfe9d | tests_generation_cost (21) | Orders 14–17: снапшоты пишутся, цена UNKNOWN | VERIFIED |
| Order economics + fee + operator rate + «Экономика заказа» | DRF-2111 | #71, #73, #74 | tests_order_economics (19) | Order 16: contribution 96,50 ₽ + «не учтено: AI без цены» | VERIFIED |
| «Метрики Pilot» + CSV/JSON export + budget 80/100 % | DRF-2111 | #72, #73, #77 | tests_pilot_analytics (28) | страница/экспорт проверены на реальных данных 09-19/20 | VERIFIED |
| Token-based ESTIMATED pricing (gpt-image-2 5/8/30, FX snapshot, immutable) | DRF-2162 | #87 → bd0dbb2 | tests_token_pricing (14) | env не задан → UNKNOWN; reconciliation не проводилась | IMPLEMENTED_NOT_LIVE_VERIFIED |
| OpenAI client timeouts (10/240/60/10) + max_retries=0 | DRF-2161 (C-0) | #82 → 675022e | tests_openai_client_timeout | действуют на staging (проверено A) | VERIFIED (config) |
| Async executor: PENDING → claim at-most-once → RUNNING, RQ worker cmd, reap_stale, dequeue, флаг off | DRF-2161 (C-1) | #85 → 972051f | tests_generation_queue (27+), pending-budget (6) | флаг off → inline; ни одного живого вызова после #85 | IMPLEMENTED_NOT_LIVE_VERIFIED |
| FULL lazy chain (один PENDING на заказ, стоп при сбое) | DRF-2161 (C-2) | PR #89 (open, 24e36fd) | tests_full_chain | — | IN_PROGRESS |
| Console async states («в очереди/генерируется», «Снять из очереди», health worker) | DRF-2161 (D-1) | ветка d1-async-console (не запушена) | tests_production_console_async (15) | — | IN_PROGRESS |
| Compose worker + deploy gate + worker_health | DRF-2161 (A-1) | #88 → 5c1ff38 | ci: compose config, bash -n | деплой 5c1ff38 в процессе; флаг off | IMPLEMENTED_NOT_LIVE_VERIFIED |
| Deploy: fetch retry (VPS→GitHub flake) | — | #75 → c6d4eea | test-resolve-deploy-ref (7) | сработал живьём 3 раза 09-19/20 | VERIFIED |
| Telegram inbound через Cloudflare Worker relay | DRF-2064 | Worker (owner) + setWebhook | — | Orders 15/17 через relay | VERIFIED |
| Outbound proxy pool (PX6 ×2, failover) | DRF-2042 | — | tests_outbound_proxy | оба PASS 2026-09-20; креды не ротированы | VERIFIED / PX6 rotation BLOCKED (owner) |
| Photo suitability gate (blur/лицо/несколько лиц/обрезка) | DRF-2164 | — | — | media.py проверяет только MIME и размер файла; Order 14 принял фото 269×576 | PLANNED |
| Visual QC hardening | DRF-2165 | — | — | только human checklist + формат | PLANNED |
| Operator attention queue | DRF-2167 | — | — | есть фильтры статус/канал/продукт/стиль; нет очереди «требует внимания» | PLANNED |
| Preview reminder / Repeat order | DRF-2168 / DRF-2169 | — | — | нет кода | PLANNED |
| Customer media lifecycle (retention/deletion) | DRF-2170 | — | — | нет кода; фото/ассеты на диске staging бессрочно | PLANNED / owner decision |
| Commercial promise sync (тексты бота ↔ реальные сроки/качество) | DRF-2171 | — | — | тексты меню из #69/#83; сроки не обещаются | UNKNOWN (нужен аудит текстов) |
| Budget alert оператору | DRF-2131 | — (дизайн B) | — | — | PLANNED (owner GO + id оператора) |
| Nodule provider (experimental, fail-closed для persona) | DRF-2072 | #41 | tests_nodule | нет entitlement | BLOCKED (capability) |

## 4. Current architecture

```
Telegram (CF Worker relay) ─┐                       ┌─ YooKassa (MAX, RUB, test-shop, DIRECT)
MAX Bot API (webhook)  ─────┴─► Django backend ◄────┴─ Telegram Stars (XTR)
   bot_menu.OrderStepper (оба канала) → channel_order_flow (Order/selection) → consent → payment adapters
   → OrderStateService (единственная точка смены статуса; OrderEvent append-only)
   → GenerationService / FullProductionService
        ├─ BudgetGuard.enforce + RUNNING-guard + cost snapshot  (одна транзакция, до провайдера)
        ├─ GenerationJob PENDING → executor.dispatch
        │     Inline (флаг off, сегодня на staging): run_job в том же процессе
        │     RQ (флаг on): Redis DB1 хранит только job id; worker `generation_worker` (compose service, 1 инстанс,
        │                   512m, grace 600s) → claim PENDING→RUNNING (select_for_update) → provider → terminal
        ├─ OpenAI images.edit через ProxyPool (timeouts 10/240/60/10, max_retries=0; в worker read 540)
        └─ stale RUNNING ≥15 мин → ambiguous (fail-closed); PENDING без worker'а → «Снять из очереди» (D-1)
   → preview approve/deliver → customer «Нравится»/revision (1) → FULL (slots; lazy chain — PR #89)
   → QC (auto checks + human checklist, normalize 512) → FinalDelivery (TG sendDocument / MAX file, at-most-once по slot)
   → DELIVERED
Console (Django admin): production/QC/delivery/economics/metrics; nginx (host 300s → compose 300s) → gunicorn 300s
```

## 5. Unit economics

Есть (dev/staging): usage per job (in/out/total + с #87 text/image split), immutable cost snapshot (PR-A), ESTIMATED по токенам
(#87: model gpt-image-2, ставки 5/8/30 USD/1M, FX snapshot, USD 6 знаков, RUB копейки half-up), per-call CONFIG_SNAPSHOT
(альтернатива), fee YooKassa (income_amount → PROVIDER_CONFIRMED), operator rate snapshot в ManualWorkLog, contribution только в RUB,
XTR → «не вычисляется», refund → «возвращено», UNKNOWN != 0 везде (C1/C2 фиксы), export без PII.

Требует проверки/действий: (1) env на staging не задан (`PILOT_*_USD_PER_1M`, `PILOT_FX_USD_RUB/DATE/SOURCE`) → все оценки UNKNOWN —
нужны owner-значения курса (источник/дата); (2) reconciliation: после первого живого вызова с ценами сравнить Σ usd_estimate
за UTC-период с OpenAI Costs (точка владельца: output 2 844 → $0.119680, согласуется с $30/M output); (3) историческая
стоимость 33 jobs без token split — UNKNOWN навсегда (по дизайну); (4) moderation/ambiguous — «возможно платные» (Order 14: 2 вызова).

## 6. Live acceptance evidence

Только факты; код после 972051f (#85 executor) живьём не генерировал.

| Date (UTC) | Channel | SKU | Deployed SHA | Order | Result |
|---|---|---|---|---|---|
| 2026-09-18 | MAX | single | ~3fdd89a…cb97683 | 11 | DELIVERED (M2) |
| 2026-09-18 | MAX | pack-9 | cb97683 | 12 | DELIVERED (M1, 9 слотов, QC retry) |
| 2026-09-18 | MAX | single (внешний клиент) | cb97683 | 13 | DELIVERED после moderation-blocked + regenerate |
| 2026-09-19 17:14 → 09-20 06:51 | MAX | single, 3D | 6cdd208 → ddfe2ce/675022e | 16 | paid 100 ₽ (fee 3,50) → preview → revision (37 с) → FULL 56 с → QC PASS → **file delivery** DELIVERED; клиент видит белый фон в превью карточки файла (сам файл прозрачный, байт-в-байт) |
| 2026-09-20 04:45 → 07:46 | Telegram | single, 3D, 2 фото | 6f89525 | 17 | Stars 100 XTR CONFIRMED → preview 38 с → approve → «Нравится» → FULL 60 с → QC PASS → sendDocument DELIVERED |
| 2026-09-20 04:43 → 05:12 | MAX | single, 3D, 2 фото | 675022e | 14 | paid; preview job 31 и 32 зависли >300 с (gunicorn kill) → ambiguous; НЕ РЕШЁН |
| 2026-09-20 05:47 | — | контролируемая генерация A (синтетика) | 675022e | — | 35 с, 2096 tokens, RGBA — новый 3D-промпт не причина зависаний |
| 2026-09-19 | Telegram | single | ea0622c | 15 | инвойс 100 XTR отправлен, не оплачен (abandoned) |

Захламление данных staging: Orders 3/8/9/10 (legacy продукт, старые сбои) висят в preview_generating/internal_preview_review/paid/QC —
нужна операторская уборка (cancel) для чистой воронки.

## 7. Open launch blockers

**P0 — блокирует контролируемый запуск**

| Linear | Почему | Dependency | Acceptance gate |
|---|---|---|---|
| DRF-2161 фоновая генерация | Order 14: синхронный вызов убит на 300 с ×2, деньги UNKNOWN; любой хвост провайдера = потерянный заказ | #89 (C-2) → D-1 → включение флага на staging (owner GO) | живой заказ через очередь: PENDING→RUNNING→asset, карточка показывает состояния; stale/dequeue проверены; ни одного двойного вызова |
| DRF-2163 Final Sticker UX | «PNG доставлен» ≠ «стикер в переписке». Telegram: клиент должен добавлять вручную через @Stickers; MAX: только бот «Стикеры в MAX» + Цифровой ID | #81 (готово) | Telegram: бот создаёт/пополняет sticker set клиента (createNewStickerSet/addStickerToSet, PNG 512) и присылает ссылку — реальный клиент отправил стикер в чат; MAX: инструкция в тексте доставки + проверка на реальном клиенте (Цифровой ID), лимит веса подтверждён |
| DRF-2054/2056 pilot acceptance на текущем коде | после #85 ни одного live-вызова; captioned SKU 0 заказов | DRF-2161, DRF-2166 | по одной живой ячейке: MAX single, TG single, MAX pack-9 (custom или без надписей) на dev ≥ 5c1ff38 |
| Owner: REAL PAID | test-shop YooKassa; PX6 не ротированы после утечки | — | PX6 ротация (DRF-2041), решение live-shop, лимиты PILOT_MAX_IMAGE_CALLS_PER_DAY/MONTH |

**P1 — до масштабирования**

| Linear | Почему | Dependency | Acceptance gate |
|---|---|---|---|
| DRF-2166 Captioned pack quality | 800 ₽ SKU без acceptance: кириллица/пунктуация/обрезка/читаемость; AI-текст vs overlay не сравнивались | DRF-2161 (FULL через очередь) | 1 живой заказ 9 с надписями: 9/9 фраз точны, читаемы, не обрезаны, QC PASS; решение AI-text vs overlay с evidence; до этого SKU не продвигать |
| DRF-2162 экономика в рублях | ставки/FX не заданы; reconciliation не проводилась | owner: курс/источник | первый live-вызов с ESTIMATED; Σ USD за период vs OpenAI Costs, расхождение объяснено |
| DRF-2164 Photo suitability gate | валидация только MIME/размер; плохой source → платный сбой (Order 14: 269×576) | — (не дублировать media.py) | до оплаты: decode, min side/лицо, blur, несколько лиц → подсказка клиенту; тесты; ложные отказы <5 % на тестовом наборе |
| DRF-2167 Operator attention queue | оператор не видит «оплачен и простаивает / ambiguous / partial / ждёт клиента» одним списком | D-1 | список с секциями и возрастом; каждое состояние из §E промпта покрыто тестом |
| DRF-2170 Customer media lifecycle | фото/ассеты хранятся бессрочно, нет удаления/анонимизации | owner/legal сроки | политика + команда retention + событие + тест; доступ только staff |
| DRF-2131 Budget alert | лимиты без уведомления | owner GO + id оператора | событие budget.alert + сообщение оператору, дедуп на окно |
| DRF-2171 Commercial promise sync | тексты бота/оферта vs реальные сроки (5–15 мин single; 9 слотов ~10+ мин + QC) | — | аудит текстов; ни одного обещания, не подтверждённого метриками |

**P2 — после первых реальных заказов**

DRF-2165 Visual QC hardening (авто-проверки likeness/edges/fingers), DRF-2168 Preview reminder, DRF-2169 Repeat order, DRF-2159,
DRF-2072 Nodule (capability), аватары, JS-статус очереди (D-3), TG test DC.

## 8. Dependency graph (фактический critical path)

```
#82 timeouts ✅ → #85 C-1 ✅ → #89 C-2 (open) ─┐
#88 A-1 worker ✅ (флаг off)                    ├─► D-1 консоль (in progress) ─► owner GO флаг ─► live cell через очередь
                                                ┘        │
DRF-2161 ──────────────────────────────────────────────► DRF-2054/2056 (live acceptance на текущем коде) ──► controlled launch
DRF-2163 (TG sticker set + MAX инструкция) ───────────►┘                                                    ▲
Параллельно (не блокируют друг друга):                                                                       │
  DRF-2162 (env + reconciliation)  DRF-2166 (captioned live)  DRF-2164 (photo gate)  DRF-2167 (queue, после D-1)
  DRF-2170 (owner decision → impl)  DRF-2171 (аудит текстов)
Owner-gated: PX6 rotation, live-shop, лимиты, курс, retention policy.
```

Отличие от ориентира владельца: DRF-2163 не зависит от DRF-2161 (доставка уже файлом; TG sticker set — отдельный Bot API
вызов), поэтому идёт параллельно, а не после; DRF-2054 требует ПОВТОРНОЙ live-acceptance после #85/#89/D-1 (код генерации изменён).

## 9. Deferred improvements

DRF-2168 Preview reminder, DRF-2169 Repeat order, DRF-2159, DRF-2165 (авто-QC), DRF-2131 после owner GO, D-3 JS-статус,
унификация формулировок денежных блоков (аудит D 09-19), TG test-DC окружение, Nodule (DRF-2072), аватары.

## 10. Owner decisions required

1. **Включение фоновой генерации на staging** (после merge #89 + D-1): да/нет и когда — это смена режима генерации.
2. **Курс USD→RUB** для DRF-2162: значение, дата, источник (внешних lookup'ов не делаем) — иначе рубли остаются UNKNOWN.
3. **REAL PAID**: ротация PX6 (DRF-2041), YooKassa test-shop → live-shop, значения `PILOT_MAX_IMAGE_CALLS_PER_DAY/MONTH`.
4. **Media retention**: сроки хранения фото клиентов/превью/финалов и правило удаления/анонимизации (юридическое решение; агенты не придумывают сроки).
5. **Captioned SKU**: разрешить один живой тестовый заказ 800 ₽ (test-shop) для acceptance до продажи.
6. **Budget alert (DRF-2131)**: GO + MAX user id / Telegram chat id оператора в env.
7. **Уборка staging-данных**: закрыть Orders 3/8/9/10 (legacy) как cancelled — оператор.

Не owner-вопросы (решаются агентами): формат TG sticker set, реализация photo gate, очередь оператора, тексты доставки.

## 11. Next execution wave (≤7 потоков)

| # | Agent | Linear | Scope | Deps | Definition of done | Merge order |
|---|---|---|---|---|---|---|
| 1 | C | DRF-2161 (C-2) | PR #89 lazy chain — review/E2E/merge | — | CI green, D E2E green, regenerate-chain regression | 1 |
| 2 | D | DRF-2161 (D-1, D-2, D-4) | консоль очереди, «Снять из очереди», health, пачка FULL, deferred-матрица | #89 | 15+ тестов, E2E inline без правок, matrix deferred | 2 |
| 3 | A | DRF-2161 (staging enable) + DRF-2162 env | verify 5c1ff38 worker idle; по owner GO — флаг on, live cell через очередь; env ставок/курса по owner | D-1 merged, owner GO | worker healthy, 1 живой заказ PENDING→DELIVERED, cost ESTIMATED записан, reconciliation план | 3 |
| 4 | B | DRF-2163 | Telegram: createNewStickerSet/addStickerToSet после delivery (PNG 512, name `<slug>_by_<bot>`), ссылка клиенту, at-most-once; MAX: текст доставки с инструкцией «Стикеры в MAX» | #81 | тесты обоих адаптеров; live: заказ типа Order 17 → набор в TG появился | 4 (независимо) |
| 5 | C (после #89) | DRF-2164 | Photo suitability gate до оплаты в media/flow: decode+dimensions+blur (Laplacian)+face count (без тяжёлых зависимостей — обосновать выбор), подсказки клиенту | — | тесты, ложные отказы на наборе примеров, без дублирования media.py | 5 |
| 6 | D (после D-1) | DRF-2167 | Operator attention queue (paid idle / queued / generating / waiting QC / waiting customer / revision / ambiguous / partial / recovery) | D-1 | каждое состояние тестом; страница в консоли | 6 |
| 7 | оркестратор | DRF-2166 / 2170 / 2171 / Linear | captioned live-acceptance план (owner GO), retention decision doc, аудит обещаний в текстах, синхронизация Linear | Linear MCP | тикеты обновлены с evidence; acceptance criteria | — |

Правила волны: один слой — один агент (C: services/generation*, D: console, B: bots/delivery adapters, A: infra/env);
merge только после review + CI + (для 1–3) E2E D; live-acceptance — только по сигналу владельца.
