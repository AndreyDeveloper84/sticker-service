# Generation Pipeline v0.1

Статус: REVIEWED

## Цель

Зафиксировать минимальный production flow для срочного MVP без переусложнения.

## Решения

Приняты:
- Preview First;
- Identity Profile;
- Identity Lock после approval preview;
- отдельный GenerationJob на каждый asset/небольшой batch;
- Human QC остаётся в MVP;
- Operator-assisted режим: оператор может править generation brief.

## Pipeline

```text
SOURCE PHOTOS
→ INPUT VALIDATION
→ IDENTITY PROFILE
→ GENERATION BRIEF
→ PREVIEW GENERATION
→ INTERNAL REVIEW
→ CUSTOMER APPROVAL / REVISION
→ IDENTITY LOCK
→ FULL PRODUCTION
→ POST-PROCESSING
→ QC
→ DELIVERY
```

## SOURCE PHOTOS

Оригиналы не перезаписываются. Фото заказа имеют статусы ACCEPTED / REJECTED / PRIMARY_REFERENCE / SECONDARY_REFERENCE.

## INPUT VALIDATION

MVP проверяет минимум:
- файл читается;
- лицо видно;
- фото достаточно качественное;
- нет явно неподходящего кадра.

При недостаточном входе заказ получает NEEDS_MORE_PHOTOS.

## IDENTITY PROFILE

На MVP это простая сущность:

```text
IdentityProfile
- source_photos
- operator_notes
- approved_preview
```

Не строим отдельный сложный биометрический пайплайн.

## GENERATION BRIEF

```text
GenerationBrief
- product
- style
- source_photos
- customer_notes
- operator_notes
- task
- approved_preview
- version
```

Brief версионируется. Ручные правки оператора сохраняются.

## PREVIEW GENERATION

Preview проверяет likeness + style до генерации полного заказа.

Рекомендуется 2–3 контрольных изображения.

## INTERNAL REVIEW

До отправки клиенту оператор может:
- APPROVE;
- REGENERATE;
- EDIT BRIEF.

## CUSTOMER APPROVAL / REVISION

Клиент подтверждает сходство либо выбирает причину правки.

Минимальные причины:
- FACE;
- HAIR;
- BODY;
- DETAIL;
- STYLE_EXPECTATION;
- OTHER.

В MVP включена одна revision.

## IDENTITY LOCK

После approval:

```text
IdentityProfile + Style + Approved Preview
→ LOCKED_FOR_PRODUCTION
```

Approved preview используется как production reference.

## FULL PRODUCTION

Полный набор не генерируется одной неделимой операцией.

Каждый asset или небольшой batch — отдельный GenerationJob:

```text
GenerationJob
- order_id
- asset_slot
- recipe_version
- attempt
- status
- provider
- cost
```

Это позволяет повторять только плохой asset.

## POST-PROCESSING

Минимум:
- crop/resize;
- прозрачность при необходимости;
- safe margins;
- форматирование под канал;
- compression.

Храним MASTER ASSET и channel-specific variants.

## QC

MVP использует ручной QC с базовыми автоматическими техническими проверками.

Проверяем:
- файл валиден;
- композиция не сломана;
- нет критических артефактов;
- сохранено сходство с approved preview;
- формат подходит для delivery.

## DELIVERY

```text
DeliveryService
→ TelegramDeliveryAdapter
или
→ MaxDeliveryAdapter
```

Core generation не зависит от канала.

## Provider abstraction

```text
GenerationService
→ ImageProvider
→ OpenAIImageProvider
```

OrderService не вызывает API модели напрямую.

## Не делаем до пилота

- отдельный AI-анализатор внешности;
- автоматический visual QC;
- сложный rule engine;
- собственную модель;
- multi-provider routing;
- обучение на пользовательских фото.
