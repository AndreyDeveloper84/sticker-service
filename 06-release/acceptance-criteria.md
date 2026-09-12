# MVP Acceptance Criteria v0.1

Статус: REVIEWED

## Release gate

MVP готов к пилоту, если оба канала проходят основной сценарий без ручного вмешательства разработчика в БД или статусы заказа.

## Обязательный happy path

```text
Telegram/MAX
→ start
→ выбор продукта
→ выбор стиля
→ загрузка фото
→ checkout
→ успешная оплата
→ preview generation
→ internal review
→ клиент approve или 1 revision
→ full production
→ QC
→ delivery
→ DELIVERED
```

## Telegram

Обязательно:
- пользователь идентифицируется;
- заказ создаётся;
- фото принимаются;
- payment success фиксируется;
- preview доставляется;
- approve/revision работает;
- финальный результат доставляется.

## MAX

Обязательно:
- пользователь идентифицируется;
- заказ создаётся;
- фото принимаются;
- payment success фиксируется;
- preview доставляется;
- approve/revision работает;
- финальный результат доставляется.

Native sticker pack для MAX не блокирует MVP.

## Production Console

Оператор может без разработчика:
- найти заказ;
- увидеть канал, продукт, стиль и фото;
- запустить preview;
- повторить generation;
- изменить operator notes/brief;
- отправить preview клиенту;
- увидеть revision;
- запустить full production;
- approve/reject QC;
- повторить failed job;
- выполнить delivery.

## State machine

Нельзя:
- запускать production до подтверждённой оплаты;
- запускать full pack до approval preview;
- доставлять результат до QC PASS.

Повторный webhook/callback не должен создавать дубль заказа или повторную оплату.

## Generation

Обязательно:
- source photos сохраняются;
- generation brief имеет версию;
- approved preview сохраняется как production reference;
- failed generation можно повторить;
- плохой asset можно перегенерировать без полного повторения заказа.

## QC

До delivery оператор подтверждает:
- нет критических визуальных дефектов;
- сохранено приемлемое сходство с approved preview;
- файлы открываются;
- формат пригоден для выбранного канала.

## Ошибки

Должны быть обработаны минимум:
- недостаточно фото;
- payment failed/cancelled;
- generation failed;
- delivery failed;
- revision requested.

Пользователь не должен видеть traceback или внутреннюю ошибку провайдера.

## Минимальная аналитика

Записываем:
- channel;
- order created;
- payment success/fail;
- preview generated;
- preview approved;
- revision requested;
- production completed;
- QC pass/fail;
- delivered;
- generation cost при доступности.

## Не блокирует пилот

- автоматический visual QC;
- Mini Apps;
- B2B;
- referrals;
- CRM;
- собственная модель;
- сложная аналитика;
- идеальный UI Production Console.

## GO

Release gate = GO, когда тестовый заказ успешно пройден end-to-end минимум один раз в Telegram и один раз в MAX, включая оплату, preview, production, QC и delivery.