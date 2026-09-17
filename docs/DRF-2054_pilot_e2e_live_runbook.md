# DRF-2054 — Live E2E runbook: dual-channel matrix for both Pilot products

Статус: **подготовка** (engineering E2E зелёный на dev `103857e`; live-прогон не выполнялся).
Владелец live-прогона: оператор Pilot + Agent A (live outbound). Документ
описывает, как прогнать ту же матрицу, что и `apps/core/tests_e2e_pilot_matrix.py`,
на staging с реальными Telegram / MAX / YooKassa / OpenAI.

## 1. Матрица

| Cell | Канал | Продукт | Объём | Цена | Оплата |
|------|-------|---------|-------|------|--------|
| T1 | Telegram | `sticker-pack-9` | 9 стикеров / 9 эмоций | 460 XTR | Telegram Stars (invoice → pre_checkout → successful_payment) |
| T2 | Telegram | `single-sticker` | 1 стикер / 1 эмоция | 100 XTR | Telegram Stars |
| M1 | MAX | `sticker-pack-9` | 9 стикеров / 9 эмоций | 500 ₽ | YooKassa (checkout URL → webhook `payment.succeeded`) |
| M2 | MAX | `single-sticker` | 1 стикер / 1 эмоция | 100 ₽ | YooKassa |

**Правило вердикта:** Pilot ready только когда все четыре ячейки пройдены до
`DELIVERED` на одном и том же деплое staging. Один канал или один продукт —
не acceptance.

Каждая ячейка: `start → product → style → emotion(s) → photos → checkout →
payment → preview → internal approve + deliver → customer approve (или
1 revision → approve) → full generation → QC → delivery`.

Рекомендованный порядок: **T2 → M2 → T1 → M1** (дешёвые single-ячейки первыми:
проверяют транспорт, оплату и формат FULL-вывода за 1 генерацию, а не за 9).

## 2. Engineering vs live

| | Engineering E2E (`manage.py test`) | Live E2E (этот runbook) |
|---|---|---|
| Telegram Bot API | `TelegramBotClient` замокан | реальный бот, webhook через staging HTTPS (STG-04) |
| MAX Bot API | `MaxBotClient` замокан | официальный MAX-бот (STG-05B) |
| YooKassa | `FakeYooKassa` (checkout + webhook object) | реальный магазин; sandbox/тестовый магазин допустим для M-ячеек, если владелец не требует боевой оплаты (см. DRF-2056) |
| OpenAI | `FakeImageProvider` (FULL → 512×512 RGBA PNG) | `OpenAIImageProvider` через outbound proxy pool (DRF-2042) |
| MAX consent / notices | `consent:accept` через webhook; PAID / production notices — мок клиента | реальные сообщения в диалоге MAX |
| Оператор | admin console через Django test client | admin console staging руками |
| Данные | test DB, `seed_live_test` | staging DB, `seed_live_test` уже применён |

Engineering E2E доказывает контракт backend/console/webhook; live доказывает
транспорт, оплату, реальный формат генерации и человеческий QC. Оба обязательны.

## 3. Предусловия

1. `origin/dev` задеплоен на staging и содержит: DRF-2050, 2057, 2051, 2052, 2053
   (delivery), 2055 (metrics), 2066 (Generate revision в консоли), MAX consent gate +
   PAID notice (PR #37), production notice, 2076 (QC normalization) — dev `103857e` и выше.
   Проверить `deploy exact SHA` и применённые миграции (`… → 0009_order_event → 0010_order_consent`).
2. Health: `curl https://<STAGING_DOMAIN>/health/` → 200; deploy run SUCCESS.
3. `.env.staging` (только SET/NOT SET, значения не печатать): `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_WEBHOOK_SECRET`, `MAX_BOT_TOKEN`, `MAX_WEBHOOK_SECRET`,
   `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `YOOKASSA_RETURN_URL`,
   `OPENAI_API_KEY`, proxy pool (DRF-2042). Webhook'и Telegram/MAX/YooKassa
   указывают на staging (STG-04 / STG-05B / DRF-1871).
   Для прохождения automated QC на реальном FULL: `OPENAI_IMAGE_BACKGROUND=transparent`
   (+ `OPENAI_IMAGE_OUTPUT_FORMAT` png/webp или не задан; PR #42) и
   `QC_NORMALIZE_FINAL_ASSETS` не установлен в `0` (DRF-2076, по умолчанию включено).
   `OPENAI_IMAGE_SIZE` задавать не нужно — 512 px даёт нормализация.
4. Каталог: `docker compose -f docker-compose.staging.yml exec backend python manage.py seed_live_test`
   → `products=sticker-pack-9, single-sticker, style=comic`; в консоли ровно два активных продукта.
5. Оператор со staff-доступом к `/admin/core/order/`.
6. Тестовые аккаунты: Telegram-аккаунт с балансом ≥ 560 XTR (T1 + T2) и
   MAX-аккаунт; для YooKassa — тестовая карта или реальная по решению владельца.
7. 2–3 фото одного человека (лицо крупно, разные ракурсы) для всех ячеек — одни и
   те же, чтобы сравнивать likeness между каналами.
8. Формат FULL-вывода. Минимальный `size` у image API — 1024, поэтому сторона 512 px
   достигается только нормализацией DRF-2076: при открытии QC report каждый FINAL asset
   fit'ится в 512×512 in place (тот же asset id; оригинал остаётся по старому ключу,
   `metadata.normalized_from` описывает исходник). Alpha нормализация **не выдумывает**:
   без `OPENAI_IMAGE_BACKGROUND=transparent` единственный ожидаемый automated FAIL —
   `missing_alpha` (gate G6 в `tests_e2e_pilot_gates.py`); `bad_dimensions` на реальном
   выводе появляться не должен — это дефект нормализации, а не провайдера.
   `OPENAI_IMAGE_OUTPUT_FORMAT=jpeg` никогда не пройдёт QC (`bad_mime_type` + `missing_alpha`).

## 4. Ожидаемые состояния по шагам (одинаковы для всех ячеек)

| # | Действие | Кто | Ожидаемое состояние / evidence |
|---|----------|-----|--------------------------------|
| 1 | `/start` (TG) / bot_started (MAX) | клиент | кнопки ровно `sticker-pack-9`, `single-sticker` |
| 2 | product → style `comic` | клиент | Order `AWAITING_PHOTOS`; pack: текст «В набор входят 9 эмоций…» + «Подтвердить набор»; single: 9 кнопок эмоций |
| 3 | emotions:confirm / emotion:`<code>` | клиент | `Order.selection.emotions` = 9 кодов / 1 код |
| 4 | 2–3 фото | клиент | `OrderPhoto` ×N, файлы в MEDIA_ROOT |
| 5 | «Фото загружены» | клиент | TG: `READY_FOR_CHECKOUT`, summary «Стикеров: 9/1», «Цена: 460/100 Stars», кнопка «Оплатить». MAX: заказ **остаётся** `AWAITING_PHOTOS`, приходит consent-экран («право использовать фотографии… обработка для стикеров… условия сервиса») с единственной кнопкой «Принимаю» (`consent:accept`); Payment ещё нет |
| 5b | «Принимаю» (только MAX) | клиент | `Order.consent_version = pilot-2026-09-v1`, `consent_accepted_at` заполнен; затем summary «Стикеров: 9/1», «500/100 ₽» и кнопка «Оплатить заказ» с URL YooKassa; Order → `AWAITING_PAYMENT`, Payment `pending yookassa/RUB`. Повторное «Принимаю» — тот же Payment/URL, consent не меняется. Без этого шага checkout невозможен |
| 6 | Оплата | клиент | TG: invoice `amount_stars` 460/100, pre_checkout ok, `successful_payment`. MAX: webhook `payment.succeeded` → клиент получает «Оплата получена. Готовим ваше превью.» ровно один раз; `Payment.metadata.paid_notice = {status: sent, message_id}`. Итог: `Payment.CONFIRMED` (`telegram_stars/XTR` или `yookassa/RUB`), Order `PAID`, ровно один CONFIRMED |
| 7 | Generate preview | оператор | `INTERNAL_PREVIEW_REVIEW`; `GenerationJob(preview)` SUCCEEDED; `output_metadata.usage` заполнен |
| 8 | Approve preview → Deliver preview | оператор | approve сам по себе не меняет статус; после deliver `PREVIEW_REVIEW`, клиент получил фото + «Как вам превью?» |
| 9a | «Нравится» | клиент | `customer_approved=true` на доставленном превью, статус остаётся `PREVIEW_REVIEW` |
| 9b | (вариант) «Нужно исправить» → категория | клиент | `REVISION_REQUESTED`, `Revision` ×1 с категорией; оператор нажимает **Generate revision** в консоли (DRF-2066) → `REVISION_GENERATING` → `INTERNAL_PREVIEW_REVIEW`, `GenerationJob(revision)` с `input_metadata.source_preview_id` = первое превью → шаг 8 → 9a. Вторая правка должна быть отклонена (409) |
| 10 | Start / resume full production ×N | оператор | по одному slot за нажатие; после 9-го/1-го — `QUALITY_CONTROL`; `GenerationJob(full)` ×9/×1 SUCCEEDED, `input_metadata.source_preview_id` = одобренное превью, `output_metadata.usage` заполнен; повторное нажатие после завершения — сообщение об ошибке, новых jobs нет. MAX: после первого нажатия клиент получает «Превью одобрено, стикеры в производстве…» ровно один раз; `Payment.metadata.production_notice = {status: sent, message_id}` |
| 11 | Открыть QC report | оператор | `QcReport attempt=1`, `expected_count` 9/1. Нормализация (DRF-2076): у каждого FINAL asset `metadata.normalized_from` с `width/height` исходника (ожидается 1024), файл по `storage_key` — 512 px PNG/WebP ≤ 512 KB, оригинал доступен по `normalized_from.storage_key`; asset ids не изменились. Automated checks: `dimensions`/`file_size`/`mime_type` ok; `alpha_channel` ok только при `OPENAI_IMAGE_BACKGROUND=transparent`, иначе единственный ожидаемый FAIL — `missing_alpha` (см. §3 п.8) |
| 12 | QC checklist (6 критериев) | оператор | все 6 отмечены → `PASSED`, Order `READY_FOR_DELIVERY`; любой снятый → `FAILED` с reason code, статус не меняется; retry slot → `PACK_GENERATING` → regenerate только этого slot → `QUALITY_CONTROL` → attempt=2 |
| 13 | Deliver final set (DRF-2053) | оператор | `DELIVERY_IN_PROGRESS` → клиент получает 9/1 **нормализованных** 512 px файлов (TG `sendDocument`, PNG с прозрачностью; MAX `send_image`) в порядке `selection.emotions` + сообщение «набор готов» → `DELIVERED`; `FinalDelivery attempt=1`, все slots `sent` с message_id. Resume после `DELIVERED` — отказ, повторных отправок нет |

## 5. Evidence per cell (собрать перед вердиктом)

- Order id, channel, product, `selection.emotions`, финальный статус.
- Payment id, provider, amount_minor, currency, external_payment_id (без секретов).
- MAX: `Order.consent_version` / `consent_accepted_at`; `Payment.metadata.paid_notice.message_id` и `production_notice.message_id` (по одному на заказ).
- GenerationJob ids: preview (и revision), 9/1 FULL; `output_metadata.usage` present.
- QcReport attempts с `automated_checks` и `reason_codes`; по одному asset — `normalized_from` (исходные width/height/mode/format, размер файла до/после).
- FinalDelivery attempt + per-slot message_id; скриншот чата клиента с 9/1 файлами.
- Deploy SHA staging и время прогона (для metrics-окна).

Шаблон:

```
CELL: T1 | deploy: <sha> | operator: <name> | date: <YYYY-MM-DD>
order=<id> product=sticker-pack-9 emotions=[...] status=DELIVERED
payment=<id> telegram_stars XTR 460 external=<charge id>
consent=<version|n/a (TG)> paid_notice=<message_id|n/a (TG)> production_notice=<message_id|n/a (TG)>
preview_job=<id> revision_job=<id|-> full_jobs=[<9 ids>]
normalized_from=<1024x1024 RGBA PNG → 512x512 PNG, e.g.> alpha=<yes|no>
qc=[attempt1: PASSED reasons=[]]
delivery=[attempt1: 9 sent + summary sent]
gaps/anomalies: <...>
VERDICT: PASS | FAIL(<stage>, <reason>)
```

## 6. Negative gates для live (минимум, после зелёных 4 ячеек)

Без новых оплат: на уже оплаченном заказе проверить, что console отклоняет
`Start full production` до «Нравится» (G3), `Открыть QC report` при
неполном наборе (G5) и `Deliver final set` до QC PASS (G6). Оплату с
неверной суммой на live не воспроизводить — покрыто engineering-gates G2.

## 7. Metrics handoff (DRF-2055)

Сразу после live-прогона снять снапшот за окно прогона:

```
python manage.py pilot_metrics --since <YYYY-MM-DD> --until <YYYY-MM-DD> --json
```

Ожидание для полной матрицы: `payments.orders_paid = 4`,
`by_provider_currency` содержит `telegram_stars/XTR` (2, 560) и `yookassa/RUB`
(2, 60000), `previews.orders_with_preview = 4`,
`approval.orders_customer_approved = 4`, `generation_cost.provider_calls_by_task_type.full = 20`
(+ revision, если использовалась), `delivery` — 4 `DELIVERED`. Снапшот приложить
к отчёту DRF-2054 и передать в DRF-2056 как baseline. Ручные минуты оператора
(`ManualWorkLog`) вносить по каждой ячейке сразу после прогона.

## 8. Известные ограничения / открытые пункты

1. Прозрачный фон зависит от `OPENAI_IMAGE_BACKGROUND=transparent` (PR #42): нормализация
   DRF-2076 alpha не создаёт. Первый реальный FULL подтверждает DRF-2076 (ожидание:
   `normalized_from` записан, `bad_dimensions` отсутствует) — до этого DRF-2076 In Progress.
2. Rollback нормализации без деплоя: `QC_NORMALIZE_FINAL_ASSETS=0` → QC видит сырой вывод
   провайдера (1024 px → `bad_dimensions`), delivery закрыт.
3. Consent-gate и PAID/production notices — только MAX; Telegram-ячейки consent не требуют.
4. Crash-window delivery (принято владельцем): после явного Resume возможен один дубль
   файла, если процесс упал между отправкой и записью message_id.
