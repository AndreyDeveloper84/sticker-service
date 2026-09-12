# Channel Adapters v0.1

Статус: DRAFT

## Цель

Изолировать Telegram и MAX от core-бизнес-логики.

## Базовый интерфейс

```text
ChannelAdapter
- identify_user()
- send_message()
- send_buttons()
- receive_media()
- send_media()
- send_preview()
- notify_status()
- deliver_order()
```

## TelegramAdapter

Отвечает за:
- Telegram identity;
- сообщения;
- кнопки;
- получение фотографий;
- уведомления;
- preview;
- delivery;
- Telegram-specific capabilities.

## MaxAdapter

Отвечает за:
- MAX identity;
- сообщения;
- кнопки;
- получение фотографий;
- уведомления;
- preview;
- delivery;
- MAX-specific capabilities.

## Правило

ChannelAdapter переводит внешние события во внутренние команды.

```text
Telegram callback
→ ApprovePreviewCommand(order_id)
→ OrderService
```

а не:

```text
Telegram callback
→ напрямую UPDATE order.status
```
