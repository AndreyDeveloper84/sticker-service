# Analytics v0.1

Статус: DRAFT

## Обязательные продуктовые события

- bot_started;
- examples_viewed;
- product_selected;
- style_selected;
- photo_uploaded;
- photos_completed;
- checkout_opened;
- payment_started;
- payment_success;
- payment_failed;
- preview_generated;
- preview_sent;
- preview_approved;
- revision_requested;
- revision_generated;
- production_started;
- production_completed;
- qc_failed;
- qc_passed;
- delivery_started;
- order_delivered.

## Обязательные разрезы

Каждое событие должно по возможности содержать:
- user_id;
- order_id;
- channel;
- product_code;
- style_code;
- timestamp.

## Основные KPI

- start → checkout conversion;
- checkout → payment conversion;
- first-preview approval rate;
- revision rate;
- generation failure rate;
- QC failure rate;
- delivery success rate;
- average order value;
- AI cost per order;
- Manual Minutes per Order;
- repeat order rate.

## Сравнение каналов

Telegram и MAX должны анализироваться отдельно по:
- конверсии;
- среднему чеку;
- отказам;
- стоимости привлечения при наличии данных;
- delivery success;
- повторным заказам.
