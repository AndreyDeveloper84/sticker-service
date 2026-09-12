# Domain Model v0.1

Статус: DRAFT

## User
```text
User
- id
- created_at
- status
```

## ChannelIdentity
```text
ChannelIdentity
- id
- user_id
- channel
- external_user_id
- username
- display_name
- created_at
```

Один User потенциально может иметь несколько ChannelIdentity.

## Product
```text
Product
- id
- code
- name
- active
- base_price
- configuration
```

## Style
```text
Style
- id
- code
- name
- active
- preview_asset
- generation_recipe_version
```

## Order
```text
Order
- id
- user_id
- channel_identity_id
- product_id
- style_id
- status
- price
- currency
- created_at
- updated_at
```

## OrderPhoto
```text
OrderPhoto
- id
- order_id
- storage_key
- status
- metadata
```

## Payment
```text
Payment
- id
- order_id
- provider
- external_payment_id
- status
- amount
- currency
- created_at
```

## GenerationJob
```text
GenerationJob
- id
- order_id
- type
- recipe_version
- status
- attempt
- provider
- created_at
```

## GeneratedAsset
```text
GeneratedAsset
- id
- order_id
- generation_job_id
- asset_type
- storage_key
- status
```

## Revision
```text
Revision
- id
- order_id
- reason
- customer_comment
- status
- created_at
```

## Delivery
```text
Delivery
- id
- order_id
- channel
- status
- external_reference
- delivered_at
```

## Главный принцип

Telegram и MAX не создают отдельные доменные модели заказа. Канал — способ взаимодействия и доставки, а не отдельная бизнес-сущность продукта.
