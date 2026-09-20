"""Russian vocabulary of the Production Console (DRF-2084).

Everything the operator reads lives here: status words for the domain
enums that keep English labels in the model (jobs, payments, QC reports,
delivery runs, production/delivery slot states), QC criteria with hints,
the «Следующий шаг» table and the translation of domain-service errors
into operator-facing sentences. Domain services and bot texts are not
touched; URL names and codes stay English.
"""

from __future__ import annotations

import re

from apps.core.models import GenerationJob, Order, Payment, QcReport, Revision

# --- time / cost warnings shown on confirmation pages ---------------------

GENERATION_WAIT = "Генерация занимает ~1–1.5 мин, не закрывайте страницу."
# Background worker (GENERATION_WORKER_ENABLED=true): the click only queues
# the attempt; the order card shows the progress and refreshes itself.
GENERATION_WAIT_ASYNC = (
    "Генерация выполняется в фоне (~1–1.5 мин, в редких случаях до 9 мин); "
    "страницу можно закрыть — результат появится в карточке заказа."
)


def generation_wait() -> str:
    from apps.core.services.generation_queue import worker_enabled

    return GENERATION_WAIT_ASYNC if worker_enabled() else GENERATION_WAIT
PAID_CALL_ONE = "1 платный вызов провайдера изображений."
PAID_CALL_PER_SLOT = "~60–90 с и 1 платный вызов на каждый слот."

# --- enum labels -----------------------------------------------------------

JOB_TASKS = {
    GenerationJob.TaskType.PREVIEW: "превью",
    GenerationJob.TaskType.REVISION: "правка",
    GenerationJob.TaskType.FULL: "производство",
}
JOB_STATUSES = {
    GenerationJob.Status.PENDING: "в очереди",
    GenerationJob.Status.RUNNING: "генерируется",
    GenerationJob.Status.SUCCEEDED: "готов",
    GenerationJob.Status.FAILED: "ошибка",
}
PAYMENT_STATUSES = {
    Payment.Status.PENDING: "ожидает оплаты",
    Payment.Status.CONFIRMED: "оплачен",
    Payment.Status.FAILED: "ошибка",
    Payment.Status.CANCELLED: "отменён",
    Payment.Status.REFUNDED: "возвращён",
}
QC_STATUSES = {
    QcReport.Status.IN_PROGRESS: "открыт",
    QcReport.Status.PASSED: "пройден",
    QcReport.Status.FAILED: "не пройден",
}
REVISION_STATUSES = {
    Revision.Status.REQUESTED: "запрошена",
    Revision.Status.GENERATING: "генерируется",
    Revision.Status.COMPLETED: "выполнена",
}
REVISION_CATEGORIES = {
    Revision.Category.FACE: "Лицо",
    Revision.Category.HAIR: "Волосы",
    Revision.Category.BODY: "Тело",
    Revision.Category.DETAIL: "Детали",
    Revision.Category.COLORS: "Цвета",
    Revision.Category.STYLE_EXPECTATION: "Стиль",
    Revision.Category.CLOTHES: "Сменить одежду",
    Revision.Category.OTHER: "Другое",
}
# FullProductionService.SlotState.status
PRODUCTION_SLOT_STATES = {
    "pending": "ожидает",
    "running": "генерируется",
    "succeeded": "готов",
    "failed": "ошибка",
}
# FinalDeliveryService.SlotDeliveryState.status / summary_status
DELIVERY_SLOT_STATES = {
    "pending": "ожидает отправки",
    "sent": "отправлен",
    "failed": "сбой",
}
DELIVERY_FAILURE_CLASSES = {
    "retryable": "временный сбой",
    "permanent": "постоянная ошибка",
}


def label(mapping, value, default=None):
    return mapping.get(value, default if default is not None else str(value))


def emotion_labels(order: Order) -> dict[str, str]:
    """slot_key (emotion code) -> customer-facing label from Product.config."""
    labels = {}
    for item in (order.product.config or {}).get("emotions") or []:
        if isinstance(item, dict) and item.get("code"):
            labels[str(item["code"])] = str(item.get("label") or item["code"])
    return labels


def custom_phrase_for_slot(order: Order, slot_key: str) -> str:
    """The customer's phrase behind a custom-pack slot («custom-N»), else ''."""
    selection = order.selection or {}
    phrases = selection.get("custom_phrases") or []
    key = str(slot_key)
    if not phrases or not key.startswith("custom-"):
        return ""
    try:
        index = int(key.rsplit("-", 1)[-1])
    except ValueError:
        return ""
    if 1 <= index <= len(phrases):
        return str(phrases[index - 1])
    return ""


def slot_title(order: Order, slot_key: str, *, max_length: int = 40) -> str:
    """«Привет» for an emotion slot, «<фраза>» for a custom-pack slot (the
    customer's text instead of «Фраза N»), else the raw code."""
    phrase = custom_phrase_for_slot(order, slot_key)
    if phrase:
        name = phrase if len(phrase) <= max_length else phrase[: max_length - 1] + "…"
        return f"«{name}»"
    name = emotion_labels(order).get(str(slot_key), str(slot_key))
    return f"«{name}»"


def customer_contact(order: Order) -> str:
    return str((order.selection or {}).get("contact") or "").strip()


def customer_contact_skipped(order: Order) -> bool:
    return bool((order.selection or {}).get("contact_skipped"))


def chat_contact(identity) -> str:
    """Where the operator can reach a customer who skipped the contact step:
    the chat the order came from — Telegram @username when there is one,
    otherwise the channel and the customer id (no other PII)."""
    channel = identity.get_channel_display()
    if identity.username:
        return f"чат {channel} @{identity.username}"
    return f"чат {channel} id {identity.external_user_id}"


def custom_phrases(order: Order) -> list[str]:
    return [str(value) for value in (order.selection or {}).get("custom_phrases") or []]


# --- QC checklist ------------------------------------------------------------

# criterion code -> (title, what to look at). Codes are the persisted
# reason codes of QcReport and never change.
QC_CRITERIA = {
    "likeness_face": (
        "Сходство лица",
        "Человек узнаваем: черты лица, взгляд, пропорции совпадают с фото.",
    ),
    "hair_edges": (
        "Волосы и края",
        "Причёска как на фото; контур без рваных краёв, ореолов и обрезанных прядей.",
    ),
    "crop": (
        "Кадрирование",
        "Персонаж целиком в кадре, ничего важного не обрезано, центрирован.",
    ),
    "transparent_background": (
        "Прозрачный фон",
        "Фон прозрачный по всему периметру, без остатков заливки и белых углов.",
    ),
    "white_outline": (
        "Белая обводка",
        "Аккуратная ровная белая обводка по контуру, без разрывов.",
    ),
    "emotion_readability": (
        "Читаемость эмоции",
        "Эмоция слота узнаётся с первого взгляда и соответствует названию.",
    ),
    "ai_artifacts": (
        "Без артефактов",
        "Нет лишних пальцев, искажённых деталей, «мусора», текста или водяных знаков.",
    ),
}

QC_AUTOMATED_CHECKS = {
    "expected_count": "полный комплект",
    "file_present": "файл на месте",
    "decodable": "файл читается",
    "mime_type": "формат PNG/WebP",
    "dimensions": "размер 512 px",
    "alpha_channel": "прозрачность",
    "file_size": "вес ≤ 512 КБ",
}

# Every human criterion the service requires must have console text;
# an unknown code falls back to itself so the checklist can always be
# completed (DRF-2084 blocker: a criterion missing here was unanswerable).
def criterion_text(code: str) -> tuple[str, str]:
    return QC_CRITERIA.get(code, (code, ""))


QC_REASON_CODES = {
    "incomplete_set": "неполный комплект",
    "missing_file": "нет файла",
    "undecodable_image": "файл не читается",
    "bad_mime_type": "неверный формат",
    "bad_dimensions": "неверный размер",
    "missing_alpha": "нет прозрачности",
    "file_too_large": "файл слишком большой",
    **{code: title for code, (title, _hint) in QC_CRITERIA.items()},
}


def reason_label(code: str) -> str:
    return QC_REASON_CODES.get(code, code)


# --- domain errors -> operator sentences ------------------------------------

PREVIEW_ALREADY_SENT = "Это превью уже отправлено клиенту"

_ERROR_PATTERNS = [
    (r"No production slots requested", "Не выбраны слоты для перегенерации."),
    (r"Unknown production slots for order #\d+: (.+)", "Неизвестные слоты: {0}."),
    (r"cannot run full production from (\S+)", "Производство нельзя запустить из статуса «{status:0}»."),
    (r"has not started full production yet", "Производство ещё не запускалось — нажмите «Запустить производство»."),
    (r"has no confirmed payment", "У заказа нет подтверждённой оплаты."),
    (r"has no customer-approved preview", "Клиент ещё не одобрил превью."),
    (r"Slots need manual intervention[^:]*: (.+)", "Слоты требуют ручной проверки (неоднозначный сбой провайдера): {0}."),
    (r"Slots are blocked[^:]*: (.+)", "Слоты заблокированы после неоднозначного сбоя: {0}. Используйте «Принудительный повтор» после ручной проверки."),
    (r"is not blocked; force retry applies only", "Слот не заблокирован — принудительный повтор нужен только для заблокированных слотов."),
    (r"is not in quality control: (\S+)", "Заказ не на контроле качества (статус «{status:0}»)."),
    (r"Nothing was regenerated since QC attempt (\d+) FAIL; regenerate slots (.+) before", "После QC-попытки {0} ничего не перегенерировано — сначала перегенерируйте слоты {1}."),
    (r"Delivery is forbidden before QC PASS", "Доставка запрещена до прохождения контроля качества."),
    (r"has no passed QC report", "У заказа нет пройденного QC-отчёта."),
    (r"Final assets changed after QC PASS", "Набор стикеров изменился после QC — контроль качества нужно пройти заново."),
    (r"Retry can be requested only for a FAILED QC report", "Доработку можно запросить только по непройденному QC-отчёту."),
    (r"Slot '(.+?)' is not a current final asset slot", "Слот {0} не входит в текущий набор."),
    (r"No slots selected for retry", "Не выбраны слоты для доработки."),
    (r"Checklist is incomplete: (.+)", "Чек-лист заполнен не полностью: {0}."),
    (r"cannot deliver final set from (\S+)", "Доставку нельзя запустить из статуса «{status:0}»."),
    (r"delivery has not started yet; use deliver\(\)", "Доставка ещё не начиналась — нажмите «Отправить набор клиенту»."),
    (r"final set is incomplete, missing slots: (.+)", "Набор неполный, нет слотов: {0}."),
    (r"emotion selection does not match product quantity (\d+)", "Выбор эмоций не совпадает с количеством стикеров в продукте ({0})."),
    (r"Delivery adapter does not match order channel", "Канал доставки не совпадает с каналом заказа."),
    (r"Approved preview was already delivered", PREVIEW_ALREADY_SENT + " — одобрите новое превью в блоке «Превью»."),
    (r"cannot generate preview from (\S+)", "Превью нельзя сгенерировать из статуса «{status:0}»."),
    (r"cannot generate revision from (\S+)", "Правку нельзя сгенерировать из статуса «{status:0}»."),
    (r"Order has no revision request", "Клиент не запрашивал правку."),
    (r"Order has no usable reference photos", "У заказа нет пригодных фотографий."),
    (r"Image provider returned empty content", "Провайдер изображений вернул пустой ответ."),
    (r"Transition (\S+) -> (\S+) is not allowed", "Переход «{status:0}» → «{status:1}» недопустим."),
    (r"moderation_blocked", "Провайдер отклонил генерацию модерацией. " + "Попробуйте ещё раз или запросите у клиента другое фото (лицо и плечи, нейтральная одежда)."),
]


def status_title(value: str) -> str:
    try:
        return str(Order.Status(value).label)
    except ValueError:
        return str(value)


MODERATION_ADVICE = (
    "Попробуйте ещё раз или запросите у клиента другое фото "
    "(лицо и плечи, нейтральная одежда)."
)


def moderation_text(details: dict) -> str:
    """RU sentence for a provider moderation rejection (DRF-2089)."""
    categories = ", ".join(details.get("moderation_categories") or []) or "не указана"
    stage = details.get("moderation_stage") or "не указана"
    text = (
        "Провайдер отклонил результат генерации модерацией "
        f"(категория: {categories}, стадия: {stage}). {MODERATION_ADVICE}"
    )
    request_id = details.get("request_id")
    if request_id:
        text += f" Request ID: {request_id}."
    return text


def job_error_text(job) -> str:
    """Operator-facing text for a FAILED GenerationJob (history line)."""
    metadata = job.output_metadata or {}
    if metadata.get("failure_class") == "moderation":
        return moderation_text(metadata)
    return job.error or ""


def humanize_error(exc: Exception) -> str:
    """Operator-facing sentence for a domain error; unknown texts are kept
    verbatim behind a Russian prefix so nothing is lost."""
    failure = getattr(exc, "failure", None)
    if isinstance(failure, dict) and failure.get("failure_class") == "moderation":
        return moderation_text(failure)
    text = str(exc)
    if text.startswith("Генерация уже"):  # already running / already queued
        return text + "."
    for pattern, template in _ERROR_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = list(match.groups())
        out = template
        for index, value in enumerate(groups):
            out = out.replace("{status:%d}" % index, status_title(value)).replace("{%d}" % index, value)
        return out
    return f"Ошибка: {text}"


# --- next step ----------------------------------------------------------------

WAITING_CUSTOMER = "Ожидаем клиента"
