# MVP Architecture v0.1

Статус: REVIEWED

## Схема

```text
Telegram Bot ─┐
              ├→ Channel Adapters → Django Core
MAX Bot ──────┘                    │
                                  ├→ Orders
                                  ├→ Media
                                  ├→ Payments
                                  ├→ Generation
                                  ├→ QC
                                  └→ Delivery
```

Хранилища и runtime:
- PostgreSQL;
- Redis;
- object storage;
- background workers;
- Docker.

## Правила

1. Один backend для Telegram и MAX.
2. Боты не меняют Order напрямую — только через core services.
3. Долгие generation jobs выполняются через очередь.
4. Изображения хранятся вне PostgreSQL.
5. Image provider скрыт за адаптером.
6. Production Console работает с тем же backend.

## Модули MVP

```text
users
products
orders
media
payments
generation
delivery
channels
operations
```

## User identity

```text
User
└─ ChannelIdentity
   ├─ TELEGRAM
   └─ MAX
```

## Адаптеры

```text
ChannelAdapter
PaymentAdapter
ImageProvider
DeliveryAdapter
```

## Production Console

Достаточно простого внутреннего UI/Django admin extension:
- список заказов;
- source photos;
- generate preview;
- regenerate/edit brief;
- revision feedback;
- generate pack;
- QC approve/reject;
- deliver/retry.

## Не строим сейчас

- microservices;
- Kubernetes;
- отдельный SPA;
- сложный workflow engine;
- собственную AI-модель.

## Первый engineering milestone

```text
Telegram/MAX
→ пользователь
→ заказ
→ фото
→ заказ виден оператору
```
