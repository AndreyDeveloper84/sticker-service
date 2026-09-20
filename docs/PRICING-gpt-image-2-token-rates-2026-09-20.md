# gpt-image-2 — официальные тарифы за токены (для OWNER confirmation, DRF-2111 ESTIMATED)

Дата снятия: 2026-09-20 ~07:15 UTC. Источник: официальная страница OpenAI
https://developers.openai.com/api/docs/pricing (раздел «Image generation token-based models», Standard processing).
Страница не публикует дату вступления тарифа — фиксируем дату снятия как pricing_version.

## Ставки gpt-image-2 (USD за 1M токенов, Standard)
| Тип токенов | Input | Cached input | Output |
|---|---|---|---|
| Text | **$5.00** | $1.25 | — |
| Image | **$8.00** | $2.00 | **$30.00** |

Batch — на 50 % дешевле (мы не используем). Отдельной цены за `images.edit` или за `input_fidelity` на странице нет —
edits тарифицируются теми же токенами (страница отсылает к калькулятору). Cached-ставки существуют, но Images API
в `usage` не сообщает cached-токены отдельно → в расчёте не участвуют (консервативно считаем всё как обычный input).

Для справки (те же таблицы): gpt-image-1 — text $5 / image $10 / output $40; gpt-image-1-mini — $2 / $2.5 / $8;
gpt-image-1.5 — $5 / $8 / output $32 (+ text output $10).

## Формула (per GenerationJob, по фактическому usage из ответа API)
```
usd = text_input_tokens  × 5.00 / 1e6
    + image_input_tokens × 8.00 / 1e6
    + output_image_tokens × 30.00 / 1e6
rub = usd × fx_usd_rub            (fx снапшотится в момент вызова: курс, дата, источник)
```
Округление: хранить USD с 6 знаками и RUB в копейках (round half up) — как OpenAI показывает cost в выгрузке ($0.119680).

## Что мы храним сейчас и чего не хватает
- Храним: `usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens` (image_providers.py:43–57).
- НЕ храним: `usage.input_tokens_details.{text_tokens, image_tokens}` — API его возвращает, а ставки text/image
  различаются ($5 vs $8) → нужно начать сохранять details (маленькая правка провайдера, без миграции).
- Исторические jobs (без details): точный split невозможен. Варианты для OWNER: (a) ESTIMATED по верхней границе
  (весь input как image, $8) с пометкой «split неизвестен» — переоценка ≤ 3 × 0,000003 $/токен; (b) оставить UNKNOWN.
  Рекомендация: (a) с явной пометкой, т.к. доля текстовых токенов мала (промпт ~200–250 токенов).

## Проверка на данных владельца
Точка из выгрузки: output tokens 2 844, actual cost $0.119680. При $30/1M output: 2 844 × 30 / 1e6 = $0.08532;
остаток $0.03436 ↔ ≈ 4 300 image-input токенов по $8 (правдоподобно для FULL с 3 референсами).
При старой ставке gpt-image-1 ($40 output) остаток был бы $0.006 ≈ 750 токенов — слишком мало для 3 картинок.
→ ставки gpt-image-2 ($8 / $30) согласуются с фактическим счётом. Точная сверка — после реализации, по UTC-периоду.

## FX (нужно решение OWNER)
Предложение: официальный курс ЦБ РФ на дату вызова (cbr.ru, XML daily) как `fx_source="cbr.ru"`, `fx_date=<дата>`;
курс снапшотится в момент вызова (кэш на сутки), при недоступности источника — RUB=UNKNOWN (USD остаётся).
Альтернатива: фиксированный курс в env `PILOT_FX_USD_RUB` + `PILOT_FX_SOURCE/DATE`, меняется вручную.

## Immutability
Снапшот в `input_metadata["cost"]` расширяется полями: model, pricing_version="openai-pricing@2026-09-20",
text_input_rate_usd_per_1m, image_input_rate_usd_per_1m, image_output_rate_usd_per_1m, fx_usd_rub, fx_date, fx_source,
cost_source=ESTIMATED; после ответа: text_input_tokens, image_input_tokens, output_tokens, usd_estimate, rub_estimate.
Ставки берутся из конфигурации (env) в момент вызова и не пересчитываются при изменении env/курса завтра
(тот же механизм, что PR-A: тест «tariff change tomorrow does not reprice old job»).

## Reconciliation (после реализации)
Сумма `usd_estimate` по jobs за UTC-период vs OpenAI Costs export за тот же период; расхождение измерить и объяснить
(cached-токены, округление, moderation-blocked вызовы без usage, ambiguous).
