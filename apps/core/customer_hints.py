"""Customer-facing hints for out-of-step bot input (DRF-2083).

Domain services raise ``ChannelFlowError`` / ``PreviewFeedbackError`` with
English, operator-oriented messages. Both bots map them here to one short
Russian hint that names the next expected action, so a customer who sends a
photo before choosing a product (or taps a stale button) is never left with
silence. Pure presentation: no domain behaviour changes.
"""

from __future__ import annotations

import re

START_HINT = "Сначала выберите продукт: нажмите /start."
CONTINUE_ORDER_HINT = (
    "У вас уже есть незавершённый заказ — продолжите его: отправьте фото "
    "или нажмите «Фото загружены»."
)
NEED_PHOTO_HINT = "Сначала отправьте хотя бы одно фото человека."
NEED_EMOTIONS_HINT = "Сначала выберите эмоции для стикера — нажмите кнопки выше."
EMOTION_CHOICE_HINT = "Этот вариант эмоции сейчас недоступен. Выберите один из предложенных выше."
PRODUCT_UNAVAILABLE_HINT = "Этот вариант недоступен. Нажмите /start и выберите продукт заново."
NO_PREVIEW_HINT = "Сейчас нет превью, ожидающего вашей оценки. Мы напишем, когда оно будет готово."
REVISION_USED_HINT = "Бесплатная правка по этому заказу уже использована. Мы продолжаем работу над стикерами."
GENERIC_HINT = "Не удалось выполнить это действие на текущем шаге. Нажмите /start, чтобы продолжить."
NEED_PHRASES_HINT = "Сначала напишите фразы для надписей — по одной в каждой строке."
NEED_CONTACT_HINT = "Оставьте имя и удобный способ связи (@username, телефон или ссылку) — не короче 3 символов."

# --- photo step (DRF-2090) ----------------------------------------------
# Live evidence (Orders 3/13): the provider's output moderation reacted to
# how the model extends a body from a reference photo. Ask for neutral
# portraits up front; remind softly when only one photo was sent.
PHOTO_GUIDANCE = (
    "Отправьте 2–3 чётких фотографии человека, лучше с разных ракурсов:\n"
    "• лицо крупно и плечи в кадре;\n"
    "• обычная повседневная одежда — без открытых плеч, декольте и купальников;\n"
    "• без солнцезащитных очков и фильтров;\n"
    "• нейтральный фон.\n\n"
    "Когда закончите, нажмите «Фото загружены»."
)
SINGLE_PHOTO_REMINDER = (
    "Лучше 2–3 фото с разных ракурсов — так стикер будет похожее. "
    "Можно отправить ещё фото сейчас или продолжить с одним."
)

# Exact domain messages → hint. Anything unknown falls back to GENERIC_HINT.
_FLOW_HINTS = {
    "No order is waiting for photos": START_HINT,
    "No order is waiting for consent": START_HINT,
    "Another order is already waiting for photos": CONTINUE_ORDER_HINT,
    "At least one photo is required": NEED_PHOTO_HINT,
    "Emotion selection is not complete": NEED_EMOTIONS_HINT,
    "Unknown emotion for this product": EMOTION_CHOICE_HINT,
    "Emotion is already selected": EMOTION_CHOICE_HINT,
    "All required emotions are already selected": NEED_PHOTO_HINT,
    "This product has no emotion selection": GENERIC_HINT,
    "Product emotion set does not match the required count": PRODUCT_UNAVAILABLE_HINT,
    "Product or style is unavailable": PRODUCT_UNAVAILABLE_HINT,
    "Product is unavailable": PRODUCT_UNAVAILABLE_HINT,
    "Order is past the consent step without consent": GENERIC_HINT,
    "This product requires custom phrases": NEED_PHRASES_HINT,
    "This product does not accept custom phrases": GENERIC_HINT,
    "Custom phrases are not complete": NEED_PHRASES_HINT,
    "A valid contact is required": NEED_CONTACT_HINT,
    # preview feedback
    "Нет превью, ожидающего вашей оценки": NO_PREVIEW_HINT,
    "No delivered approved preview": NO_PREVIEW_HINT,
    "Order is not awaiting preview feedback": NO_PREVIEW_HINT,
    "Included preview revision has already been used": REVISION_USED_HINT,
    "Unknown revision category": GENERIC_HINT,
}


_PHRASE_COUNT = re.compile(r"^Exactly (\d+) custom phrases are required$")


def phrases_count_hint(required: int, received: int) -> str:
    return (
        f"Нужно ровно {required} фраз — по одной в каждой строке, одним сообщением. "
        f"Сейчас строк: {received}. Отправьте список ещё раз."
    )


def customer_hint(exc: Exception, *, received_lines: int | None = None) -> str:
    """Short Russian hint for a domain flow/feedback error."""
    text = str(exc)
    match = _PHRASE_COUNT.match(text)
    if match:
        return phrases_count_hint(int(match.group(1)), received_lines if received_lines is not None else 0)
    return _FLOW_HINTS.get(text, GENERIC_HINT)
