# Production Console v0.1

Статус: DRAFT

## Цель

Единая рабочая поверхность оператора для заказов из Telegram и MAX.

## Очереди

Оператор должен видеть минимум:

- новые;
- ждут preview;
- ждут клиента;
- нужна правка;
- в производстве;
- QC;
- готовы;
- failed.

## Карточка заказа

```text
ORDER #184

Channel: Telegram / MAX
Product
Style
Payment status
Order status

SOURCE PHOTOS

CUSTOMER REQUIREMENTS

GENERATION
- recipe
- attempt
- job status

PREVIEW

CUSTOMER FEEDBACK

PRODUCTION

QC

DELIVERY
```

## Основные действия

- открыть заказ;
- посмотреть source photos;
- запустить preview generation;
- перегенерировать;
- изменить generation parameters в допустимых границах;
- подтвердить preview для отправки;
- увидеть customer revision;
- запустить revision;
- запустить full production;
- принять/отклонить generated assets;
- подтвердить QC;
- инициировать delivery;
- повторить failed job.

## Принцип

Оператор управляет производством, а не вручную синхронизирует Telegram и MAX.

Канал должен быть виден как атрибут заказа, но не определять операторский workflow.
