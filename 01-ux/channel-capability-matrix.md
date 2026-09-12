# Channel Capability Matrix v0.2

Статус: REVIEWED

Цель документа — зафиксировать продуктовый паритет Telegram и MAX, не требуя одинаковой технической реализации.

| Возможность | Telegram | MAX | Решение |
|---|---|---|---|
| Запуск бота | Да | Да | Реализовать |
| Inline-кнопки / callbacks | Да | Да | Реализовать через channel adapter |
| Загрузка фотографий | Да | Да | Реализовать |
| Отправка preview | Да | Да | Реализовать |
| Revision flow | Да | Да | Общая core-логика |
| История заказов | Да | Да | Хранится в backend |
| Оплата цифрового товара | Telegram Stars | Отдельный MAX payment flow | Разделить payment adapters |
| Выдача изображений | Да | Да | Реализовать |
| Native user sticker set | Да: Bot API умеет createNewStickerSet | Не подтверждено открытым Bot API | Core не должен зависеть от native pack |
| Deep links | Да | Да | Channel-specific реализация |
| Mini App | Да | Да | Не обязателен в первом MVP |
| Production event delivery | Webhook / Bot API updates | Webhook рекомендуется для production | Не строить core вокруг polling |

## Telegram-specific

- Цифровые товары и услуги внутри Telegram оплачиваются через Telegram Stars (`XTR`).
- После успешной оплаты необходимо хранить `telegram_payment_charge_id` для возможного возврата.
- Бот должен поддерживать `/paysupport`.
- Для sticker pack целевой delivery использует Bot API `createNewStickerSet`/`addStickerToSet`.

## MAX-specific

- Production-интеграция использует API на `platform-api2.max.ru`.
- Для production MAX рекомендует Webhook; long polling имеет ограничения.
- Inline keyboard и callback-события доступны.
- Изображения и файлы можно отправлять через Bot API.
- Mini App работает только в связке с чат-ботом.
- Подключение ботов и Mini Apps требует верифицированного профиля организации, ИП или самозанятого — резидента РФ.
- До появления официально подтверждённого метода создания пользовательского sticker set delivery проектируется как независимая capability.

## Архитектурное правило

Core backend не зависит напрямую от API Telegram или MAX.

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

Платежи также изолируются:

```text
PaymentAdapter
- create_payment()
- verify_payment()
- refund_payment()
```

Channel-specific возможности реализуются расширениями адаптеров, а не условиями по всему core-коду.
