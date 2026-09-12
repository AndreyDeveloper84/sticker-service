# Sticker Service — Product & Architecture Docs

Документация для сервиса персонализированных стикеров и аватаров.

## Зафиксированные решения

- MVP сразу поддерживает Telegram Bot и MAX Bot.
- Это один продукт и один backend.
- Telegram и MAX подключаются через отдельные channel adapters.
- Первые продукты: Personal Sticker Pack и Personal Avatar.
- Используется preview-first flow.
- На MVP сохраняется Human-in-the-loop.
- Оператор работает через единую Production Console.

## Структура

```text
00-product/
  concept.md
  mvp-scope.md
  product-catalog.md

01-ux/
  customer-journey.md
  channel-capability-matrix.md
  order-state-machine.md

02-domain/
  domain-model.md

04-architecture/
  system-architecture.md
  channel-adapters.md

05-operations/
  production-console.md
  analytics.md
```

## Следующие документы

- telegram/screen-inventory.md
- telegram/dialogs.md
- max/screen-inventory.md
- max/dialogs.md
- commercial-rules.md
- generation-pipeline.md
- prompt-style-playbook.md
- qc-spec.md
- integrations.md
- data-storage-and-privacy.md
- acceptance-criteria.md

## Статусы документов

- DRAFT
- REVIEWED
- FROZEN
