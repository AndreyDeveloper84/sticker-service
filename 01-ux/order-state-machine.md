# Order State Machine v0.1

Статус: DRAFT

## Основные состояния заказа

```text
DRAFT
AWAITING_PHOTOS
READY_FOR_CHECKOUT
AWAITING_PAYMENT
PAID
PREVIEW_GENERATING
INTERNAL_PREVIEW_REVIEW
PREVIEW_REVIEW
REVISION_REQUESTED
REVISION_GENERATING
PACK_GENERATING
QUALITY_CONTROL
READY_FOR_DELIVERY
DELIVERY_IN_PROGRESS
DELIVERED
CANCELLED
FAILED
REFUND_PENDING
REFUNDED
```

## Базовый happy path

```text
DRAFT
→ AWAITING_PHOTOS
→ READY_FOR_CHECKOUT
→ AWAITING_PAYMENT
→ PAID
→ PREVIEW_GENERATING
→ INTERNAL_PREVIEW_REVIEW
→ PREVIEW_REVIEW
→ PACK_GENERATING
→ QUALITY_CONTROL
→ READY_FOR_DELIVERY
→ DELIVERY_IN_PROGRESS
→ DELIVERED
```

## Revision path

```text
PREVIEW_REVIEW
→ REVISION_REQUESTED
→ REVISION_GENERATING
→ INTERNAL_PREVIEW_REVIEW
→ PREVIEW_REVIEW
```

## Основные правила

- Production нельзя запускать до PAID.
- Full pack нельзя запускать до approval preview.
- Revision не должен изменять заказ произвольно.
- Delivery возможен только после успешного QC.
- FAILED не означает автоматическую отмену заказа.
- Канал не управляет бизнес-состоянием напрямую; он отправляет команды в core.

## Что ещё нужно определить

- допустимое число revision;
- таймауты оплаты;
- отмена пользователем;
- автоматические retry;
- возвраты;
- правила восстановления failed jobs.
