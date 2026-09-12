# Channel Capability Matrix v0.1

Статус: DRAFT

| Возможность | Telegram | MAX | Решение |
|---|---|---|---|
| Запуск бота | Да | Да | Реализовать |
| Выбор продукта | Да | Да | Реализовать |
| Кнопки / действия | Да | Да | Реализовать |
| Загрузка фотографий | Да | Да | Реализовать |
| Отправка preview | Да | Да | Реализовать |
| Revision flow | Да | Да | Реализовать |
| История заказов | Да | Да | Через backend |
| Оплата | Отдельная интеграция | Отдельная интеграция | Payment adapters |
| Выдача изображений | Да | Да | Реализовать |
| Native sticker pack | Channel-specific | Требует проверки | Не связывать core |
| Deep links | Channel-specific | Channel-specific | Через adapter |
| Mini App | Возможен | Возможен | Не обязателен для MVP |

## Архитектурное правило

Core backend не должен напрямую зависеть от Telegram API или MAX API.

```text
ChannelAdapter
- send_message()
- send_media()
- request_photo()
- send_preview()
- notify_status()
- deliver_order()
```
