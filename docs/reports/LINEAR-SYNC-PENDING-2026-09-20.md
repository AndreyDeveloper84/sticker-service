# Linear sync — pending (Linear MCP недоступен весь день 2026-09-20; применить при восстановлении)

Правило: статус Done только при выполненном acceptance; иначе In Progress + список недостающего evidence.

| Issue | Предлагаемый статус | Комментарий (что реализовано / SHA / где проверено / evidence / что осталось) |
|---|---|---|
| DRF-2161 async generation | **Done** (кандидат) | Код: #82 timeouts (675022e), #85 C-1 executor/claim/reap/dequeue (972051f), #89 C-2 lazy chain (в 606f3c2), #92 D-1 консоль (606f3c2), #88 compose worker (5c1ff38). Staging 69cfc79, флаг ON 10:07Z. Live: Order 14 — job 36 preview PENDING→claim 130 мс→RUNNING→SUCCEEDED 38,8 с; job 37 FULL 57,9 с; один job/один вызов, RQ result discarded, provider из web не вызывается (WorkerEnabledWebInvariantTests), stale RUNNING job 32 → ambiguous на GET карточки без повторного вызова; Budget Guard считает PENDING (tests_pilot_budget_pending). Redeploy 69cfc79: worker пересоздан один (фикс. имя), in-flight 0. Не проверено живьём: dequeue PENDING (нет кандидата без worker'а), FULL 9 слотов последовательно/стоп цепочки (только тесты tests_full_chain; ждём pack-9 live). |
| DRF-2162 token pricing | In Progress → Done после reconciliation | #87 (bd0dbb2). Staging pricing ON 10:15Z: gpt-image-2, openai-pricing@2026-09-20, 5/8/30, FX 84.1975/2026-09-19/CBR. Live: job 36 $0.043269 = 3,64 ₽; job 37 $0.086995 = 7,32 ₽ (формула проверена вручную); консоль «≈ … ₽ — Оценка по фактическому usage (…)». Старые jobs UNKNOWN. Осталось: сверка Σ USD (0.130264 за 2026-09-20 UTC по jobs 36+37) с OpenAI Costs. |
| DRF-2163 Final Sticker UX | In Progress (TG live ✔, MAX usage не доказан) | #91 (681bb03). TG: Order 17 → набор sticks_605943742_by_StickersForYouF_bot создан, ссылка отправлена (msg 110), владелец подтвердил добавление и отправку стикера в чате. MAX: файл прозрачный (байт-в-байт, MAX API), инструкция в тексте доставки (Order 14 delivered 12:05Z с новым текстом); НЕ доказано: реальное создание набора в «Стикеры в MAX» (Цифровой ID) и отправка в переписке. Устройство/версии клиентов — не зафиксированы. |
| DRF-2164 Photo gate | In Progress (не закрывать) | #93 (606f3c2): decode/min side 512/aspect 2.5/bomb enforce, blur observe. Live-метрик пока 0 (новых фото после деплоя не было). Не покрыто: лицо/размер лица/несколько лиц, false-reject на реальных фото → follow-up DRF-2164-b после ≥20 живых фото. |
| DRF-2167 Attention queue | **Done** | #94 (69cfc79). Staging: страница 200, корзины совпали с ожиданием (14 recovery, 9 paid_idle, 8 operator, 10 QC, 3 error); «Закрыть заказ» применён владельцем: 3/8/10 → failed, 9 → cancelled, order.closed events, ничего не удалено; после этого очередь пуста (все 0). |
| DRF-2148 Bot navigation | **Done** | #83 (ea0622c) + #84 контакт необязателен (860c7b4); live Order 17 доведён после деплоя. |
| DRF-1871 live payments/preview smoke | In Progress | YooKassa test-shop: Orders 14/16 (fee 3,50 ₽); Stars: Order 17 100 XTR paid + refund (Payment 17 REFUNDED, event). Осталось: live-shop YooKassa (owner), PX6 rotation. |
| DRF-2054 dual-channel E2E | In Progress | Live на текущем коде: MAX single Order 14 (69cfc79, через worker) ✔; TG single Order 17 (6f89525, до worker'а). Осталось: TG single на ≥69cfc79, MAX pack-9 500 ₽, TG pack-9 460 XTR. |
| DRF-2056 pilot checklist | In Progress | см. baseline §7; owner decisions: PX6, live-shop, лимиты, retention. |
| DRF-2166 captioned | Todo | 0 живых заказов; SKU не рекламировать. |
| DRF-2170 media lifecycle | In Progress | Agent B, PROMPT-AGENT-B-DRF-2170-MEDIA-LIFECYCLE-2026-09-20.md (retention off by default, purge on request). |
| DRF-2171 promise sync | Todo | аудит текстов не начат. |
| DRF-2160 (сводный quality wave) | Done | #75–#86 (см. описание). |
| DRF-2131 budget alert | Backlog | ждёт owner GO + id оператора. |
