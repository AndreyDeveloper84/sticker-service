# Product Catalog v0.1

Статус: DRAFT

## P01 — Personal Sticker Pack

Персональный набор стикеров по фотографиям одного человека.

Базовая конфигурация MVP:
- один человек;
- один стиль;
- один sticker pack;
- 12–16 стикеров как рабочий ориентир;
- один preview;
- одна корректировка preview;
- финальный QC;
- выдача результата через Telegram или MAX.

Режимы содержимого:
- Standard Pack — системный набор эмоций и реплик;
- Custom Pack — пользовательские короткие фразы или идеи.

## P02 — Personal Avatar

Персональный аватар по фотографиям пользователя.

Рабочий ориентир:
- один человек;
- один стиль;
- 3 варианта;
- один preview;
- одна корректировка;
- финальная выдача.

## Product

```text
Product
- id
- code
- name
- description
- active
- base_price
- output_quantity
- preview_quantity
- included_revisions
- min_source_photos
- recommended_source_photos
- max_source_photos
- allowed_styles
- generation_pipeline
- delivery_type
```

## Style

```text
Style
- id
- code
- name
- description
- preview_image
- active
- sort_order
- generation_recipe_version
```

До анализа реальных заказов не замораживаются цена, точное количество стикеров и фото, список эмоций, лимит custom-фраз, полный каталог стилей и правила дополнительных revision.
