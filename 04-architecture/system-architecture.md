# System Architecture v0.1

Статус: DRAFT

## Общая схема

```text
Telegram Bot ─┐
              ├── Channel Layer ──→ Core Backend
MAX Bot ──────┘                     │
                                    ├── User Service
                                    ├── Order Service
                                    ├── Product Service
                                    ├── Payment Service
                                    ├── Media Service
                                    ├── Generation Service
                                    ├── Revision Service
                                    ├── QC Service
                                    └── Delivery Service
                                           │
                         ┌─────────────────┼──────────────────┐
                         ▼                 ▼                  ▼
                    PostgreSQL          Redis             Object Storage
                                           │
                                           ▼
                                      Job Queue / Workers
                                           │
                                           ▼
                                      AI Provider Layer
```

## Принципы

1. Один backend для Telegram и MAX.
2. Каналы не содержат бизнес-логику.
3. Генерация не зависит от канала.
4. Платёжные интеграции изолированы.
5. Delivery реализуется через channel adapters.
6. Медиа хранятся вне БД в object storage.
7. Долгие операции выполняются через очередь задач.
8. Все ключевые действия логируются и доступны для аудита.

## Базовый стек

Предварительный ориентир:
- Python;
- Django;
- Django REST Framework;
- PostgreSQL;
- Redis;
- background workers;
- object storage;
- Docker.

Точный worker/queue и storage provider фиксируются позже.

## Channel Layer

```text
TelegramAdapter
MaxAdapter
```

Базовый контракт:

```text
send_message()
send_media()
send_preview()
notify_status()
deliver_order()
```

## Generation Layer

```text
GenerationService
→ AIProviderAdapter
→ GeneratedAsset
```

Generation Service не знает, из какого канала пришёл заказ.

## Production Console

Единая операторская поверхность для заказов из обоих каналов.

Она работает с Core Backend, а не напрямую с Telegram/MAX.
