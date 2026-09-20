# DRF-2170 — Customer Media Lifecycle (2026-09-20)

Минимальный, доказуемый жизненный цикл клиентских медиа. Сроки хранения **не выдуманы**: их задаёт владелец через env;
по умолчанию автоудаление **выключено**. Код: `apps/core/services/media_lifecycle.py`, команда `media_retention`,
консольное действие «Удалить медиа клиента…», тесты `apps/core/tests_media_lifecycle.py`.

## 1. Инвентаризация: что и где лежит

| Объект | Где | Что содержит | Удаляется? |
|---|---|---|---|
| `OrderPhoto` (source photos) | файл `MEDIA_ROOT/orders/<order>/source/…` (`storage_key`), строка в БД (`original_filename`, `mime_type`, `size_bytes`, `metadata`) | фото клиента (биометрически чувствительно) | **файл — да** (retention / запрос); строка остаётся с `metadata.purged` |
| `GeneratedAsset` preview | `MEDIA_ROOT/generated/order-<order>/preview/…` | превью с лицом клиента | **файл — да**; строка остаётся |
| `GeneratedAsset` final | `MEDIA_ROOT/generated/order-<order>/final/…` + **provider-оригинал** до QC-нормализации (`metadata.normalized_from.storage_key`) | готовые стикеры (лицо клиента) | **оба файла — да**; строка и `normalized_from` (аудит размеров) остаются |
| `Order.selection.contact` | БД (JSON) | имя/@username/телефон, введённые клиентом | **да, по запросу клиента** (`contact=""`, `contact_purged_at`); retention не трогает |
| `ChannelIdentity` | БД | `external_user_id` (Telegram/MAX id), `username`, `display_name` | нет — нужен для доставки/возвратов/связи; owner decision на будущее |
| `OrderEvent.payload` | БД | статусы, минуты, суммы, причины (`manual.work_logged.note`, `payment.refunded.reason`, `media.purged`) — без фото, без контакта | нет (accounting trail); `media.purged` содержит только счётчики/байты/правило |
| `Payment`, `GenerationJob` (+ `input_metadata.cost`), `QcReport`, `FinalDelivery` | БД | платежи, снапшоты стоимости, QC, доставка (message ids) | **никогда** |
| Логи (gunicorn/Django) | stdout контейнера | `order=<id>`, статусы, коды ошибок, `sticker_set.skipped`, `media_lifecycle.purged order=… files=… bytes=…`; **нет** контактов, имён файлов клиента и токенов (см. `telegram.network_error` — только тип исключения) | вне scope |
| Бэкапы БД | `deploy/scripts/backup.sh` → `./backups/*.sql.gz` на VPS (pg_dump перед каждой миграцией/деплоем) | **вся БД**, включая `selection.contact`, `ChannelIdentity`, `OrderEvent`; **медиа-файлы в бэкапы не входят** | ротация — **owner decision**, в этом PR не реализуется |
| Доступ к файлам | admin file views `core_orderphoto_file`, `core_preview_asset_file` | требуют staff-логин (`admin_view`): `tests_media.test_staff_can_retrieve_saved_file_through_admin`, `tests_media_lifecycle.AccessTests` (аноним → redirect на логин) | — |
| Экспорт метрик §21 | CSV/JSON «Метрики Pilot» | без PII: `tests_pilot_analytics.test_export_csv_columns_unknown_empty_no_pii` | — |

## 2. Политика retention (env, всё опционально)

```
MEDIA_RETENTION_ENABLED=false            # default: автоудаление выключено
MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=      # owner decision; пусто = хранить
MEDIA_RETENTION_PREVIEWS_DAYS=           # owner decision; пусто = хранить
MEDIA_RETENTION_FINALS_DAYS=             # owner decision; пусто = хранить
```

Правила:
- отсчёт — от момента, когда заказ стал **терминальным** (последний `order.status_changed` в DELIVERED / FAILED / CANCELLED; без события — `updated_at`);
- удаляются **только** медиа терминальных заказов; незавершённые не трогаются никогда;
- `Payment` / `GenerationJob` (со снапшотом стоимости) / `OrderEvent` / `QcReport` / `FinalDelivery` не удаляются;
- в `OrderPhoto.metadata` / `GeneratedAsset.metadata` остаётся `purged = {at, rule, reason_ref, attempts, size_bytes, actor_ref}` — «файл удалён <когда> <по правилу>»; файл на диске удаляется (для финала — и provider-оригинал); у `OrderPhoto` после успешного удаления стирается `original_filename` (имя файла клиента — не accounting);
- **файл, который не удалось удалить (ошибка ФС), никогда не помечается `purged`**: строка получает `purge_failed = {attempts, last_error («Класс: сообщение» без путей/PII, ≤200), last_attempt_at, rule, reason_ref}`, остаётся в `media_items()` / следующем плане retention и повторяется следующим запуском (команда или консоль); успех → `purged` (с `attempts`), `purge_failed` снимается;
- невалидное/отрицательное значение срока → вид хранится (warning в лог), никакого срока по умолчанию;
- на каждый запуск по заказу — событие `OrderEvent media.purged` `{rule, reason (код), reason_note (маскированный текст), status: complete|partial, counts{source_photos, previews, finals}, files, errors, remaining, bytes, contact_cleared}` — без имён файлов и контактов; при повторной попытке — новое событие (нужна истина, не at-most-once).
- отчёт оператору/команде — «успех» только когда все файлы удалены; иначе «частично: осталось N файлов, повторите» (консоль — WARNING).

## 3. Команда

```
manage.py media_retention            # dry-run: печатает политику и план (заказы, виды, файлы, байты), ничего не удаляет
manage.py media_retention --apply    # удаляет по плану; отказ (CommandError), если MEDIA_RETENTION_ENABLED != true
```
Запуск по расписанию (cron на VPS) — после решения владельца о сроках; в этом PR не настраивается.

## 4. Запрос клиента на удаление

`MediaLifecycleService.purge_order(order, reason=<код>, note=…, actor_ref=…)` и консольное действие **«Удалить медиа
клиента…»** (блок «Дополнительно» карточки заказа; только суперпользователь; только терминальные заказы): причина —
из фиксированного словаря (`customer_request` / `legal` / `operator_other`), свободный комментарий необязателен,
обрезается до 120 символов, e-mail / телефон / @username маскируются `***` и хранится **только в событии**, не в
metadata. Удаляет фото / превью / финалы (+ оригиналы) сразу, независимо от возраста, стирает `selection.contact`;
accounting остаётся; событие `media.purged` с `rule=customer_request`. Уже удалённые объекты пропускаются; объекты с
`purge_failed` повторяются; пока остаются файлы — статус `partial` и предупреждение оператору.

### Что остаётся в БД после удаления и почему
- `OrderPhoto` / `GeneratedAsset` строки с `storage_key` — идемпотентность (по ключу видно, что удалять больше нечего),
  аудит размеров/форматов (`normalized_from`, `size_bytes`) и связь с `GenerationJob` (accounting); сам ключ — путь
  вида `orders/<id>/source/<uuid>.jpg`, без имени клиента;
- `original_filename` у `OrderPhoto` — **стирается** при успешном удалении (реализовано; owner decision — оставить так);
- `actor_ref` в событии/metadata — username **оператора**, не клиента.

## 5. Owner decisions (не решены в коде)

1. Сроки хранения по типам: source photos / previews / finals (дни) — и включать ли `MEDIA_RETENTION_ENABLED`.
2. Срок хранения бэкапов БД на VPS (`./backups/*.sql.gz` содержат `selection.contact` и identity) и их ротация.
3. Шаблон ответа клиенту на запрос удаления (что сообщаем: удалены фото/превью/стикеры и контакт; платёжные данные
   хранятся по требованиям учёта; идентификатор чата остаётся для связи по заказу).
4. Судьба `ChannelIdentity` (id/username) после удаления медиа — сейчас сохраняется.
5. Очищать ли `original_filename` при purge — реализовано как «да» (стирается после успешного удаления файла);
   подтвердить или отменить.
