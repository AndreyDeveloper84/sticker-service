"""Shared customer-facing menu and order-step screens for Telegram and MAX.

One channel-agnostic step engine: every screen is ``(text, rows)`` where
``rows`` is a list of button rows and every button is ``(label, payload)``.
The bots only render buttons in their own widget format and route the
payloads back here, so the dialog is identical in both channels.

Dialog (owner contract): main menu → product → style → [emotions for the
standard products] → photos → [9 phrases for the captioned pack] → name and
contact → order card → «✅ Подтвердить заказ» → consent → payment.
Every intermediate step carries «⬅️ Назад» and «🏠 Главное меню».
"""

from __future__ import annotations

import re

from apps.core.customer_hints import PHOTO_GUIDANCE, SINGLE_PHOTO_REMINDER, customer_hint
from apps.core.models import Order
from apps.core.services.channel_order_flow import (
    ChannelFlowError,
    order_custom_phrases,
    product_emotion_count,
    product_requires_custom_phrases,
)

# --- button labels / payloads ---------------------------------------------

BACK_LABEL = "⬅️ Назад"
HOME_LABEL = "🏠 Главное меню"
CONFIRM_ORDER_LABEL = "✅ Подтвердить заказ"
PHOTOS_DONE_LABEL = "Фото загружены"

PAYLOAD_MENU = "menu:main"
PAYLOAD_ORDER = "menu:order"
PAYLOAD_BACK_STYLE = "back:style"
PAYLOAD_BACK_EMOTIONS = "back:emotions"
PAYLOAD_BACK_PHOTOS = "back:photos"
PAYLOAD_BACK_PHRASES = "back:phrases"
PAYLOAD_BACK_CONTACT = "back:contact"
PAYLOAD_PHOTOS_DONE = "photos_done"
PAYLOAD_CONFIRM_ORDER = "order:confirm"

# Order.selection["awaiting_input"] values while the bot waits for free text.
AWAITING_PHRASES = "phrases"
AWAITING_CONTACT = "contact"

# --- menu texts ---------------------------------------------------------------

MAIN_MENU = "Главное меню — выберите, что вас интересует:"
PRODUCTS_TITLE = "Выберите вариант:"
STYLES_TITLE = "Выберите стиль:"
PHRASES_PROMPT = (
    "Напишите 9 фраз для надписей на стикерах — по одной в каждой строке "
    "(одно сообщение, 9 строк). Например: «Доброе утро», «Уже бегу», «Спасибо!»."
)
CONTACT_PROMPT = (
    "Оставьте имя и удобный способ связи — @username, телефон или ссылку. "
    "Например: «Анна, @anna». Мы напишем только по вашему заказу."
)
PHOTO_SAVED = "Фото сохранено. Отправьте ещё или нажмите «Фото загружены»."

RESULT_REQUIREMENTS = (
    "Готовые стикеры: PNG или WEBP, одна сторона ровно 512 px, вторая — не больше 512, "
    "прозрачный фон, вес до 512 КБ, чёткие, голова, волосы, руки и надписи не обрезаны, "
    "аккуратная белая обводка."
)
PHOTO_REQUIREMENTS = f"📸 Требования к фото\n\n{PHOTO_GUIDANCE}\n\n{RESULT_REQUIREMENTS}"
ORDER_HOW_IT_WORKS = (
    "❓ Как проходит заказ\n\n"
    "1. Выбираете вариант и стиль, присылаете 2–3 фото.\n"
    "2. Для набора с надписями пишете 9 фраз, затем оставляете имя и контакт.\n"
    "3. Проверяете карточку заказа, подтверждаете и оплачиваете.\n"
    "4. Мы присылаем превью — можно принять или попросить одну правку.\n"
    "5. Делаем весь набор, проверяем качество и присылаем готовые стикеры сюда."
)
EXAMPLES_TEXT = (
    "🖼 Примеры работ скоро появятся здесь. Пока можно сразу выбрать стиль — "
    "на превью вы увидите, как будет выглядеть ваш персонаж."
)
CONTACT_TEXT = (
    "💬 Связаться со мной\n\n"
    "Нажмите «Заказать стикеры» и на шаге контакта оставьте имя, @username, "
    "телефон или ссылку — мы свяжемся по вашему заказу. Вопрос без заказа можно "
    "написать прямо здесь, в этом чате."
)

_PRICE_SUFFIX = re.compile(r"\s*[—–-]\s*\d[\d\s]*\s*(₽|руб\.?|Stars|XTR)\s*$", re.IGNORECASE)


def product_title(product) -> str:
    """Product name without a trailing price («… — 800 ₽») so the price is
    printed once, from Product.config."""
    return _PRICE_SUFFIX.sub("", str(product.name)).strip()


def product_price_label(product, *, telegram: bool) -> str:
    config = product.config or {}
    if telegram:
        stars = config.get("price_stars")
        if isinstance(stars, int) and not isinstance(stars, bool) and stars > 0:
            return f"{stars} Stars"
        return "цена уточняется"
    try:
        minor = int(config.get("price_minor") or 0)
    except (TypeError, ValueError):
        minor = 0
    return f"{minor // 100} ₽" if minor > 0 else "цена уточняется"


def prices_text(products, *, telegram: bool) -> str:
    """«💰 Цены» from the active products' config — never hard-coded."""
    lines = [f"{product_title(product)} — {product_price_label(product, telegram=telegram)}" for product in products]
    if not lines:
        lines = ["Сейчас нет доступных вариантов — загляните позже."]
    return "💰 Цены\n\n" + "\n".join(lines)


# --- navigation -----------------------------------------------------------------

def nav_rows(back_payload: str | None = None) -> list[list[tuple[str, str]]]:
    rows = []
    if back_payload:
        rows.append([(BACK_LABEL, back_payload)])
    rows.append([(HOME_LABEL, PAYLOAD_MENU)])
    return rows


def main_menu_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🎨 Заказать стикеры", PAYLOAD_ORDER)],
        [("💰 Цены", "menu:prices")],
        [("🖼 Примеры работ", "menu:examples")],
        [("📸 Требования к фото", "menu:photos")],
        [("❓ Как проходит заказ", "menu:how")],
        [("💬 Связаться со мной", "menu:contact")],
    ]


# --- screens -----------------------------------------------------------------------

def main_menu_screen():
    return MAIN_MENU, main_menu_rows()


def products_screen(products, *, telegram: bool):
    rows = [
        [(f"{product_title(product)} — {product_price_label(product, telegram=telegram)}", f"product:{product.code}")]
        for product in products
    ]
    return PRODUCTS_TITLE, rows + nav_rows()


def styles_screen(product_code: str, styles):
    rows = [[(style.name, f"style:{product_code}:{style.code}")] for style in styles]
    return STYLES_TITLE, rows + nav_rows(PAYLOAD_ORDER)


def emotions_screen(options: list[dict], required: int):
    """Standard products only: the pack confirms its deterministic set, the
    single sticker picks one emotion."""
    if required == len(options):
        labels = ", ".join(option["label"] for option in options)
        text = f"В набор входят {required} эмоций: {labels}."
        rows = [[("Подтвердить набор", "emotions:confirm")]]
    else:
        text = "Выберите эмоцию для стикера."
        rows = [[(option["label"], f"emotion:{option['code']}")] for option in options]
    return text, rows + nav_rows(PAYLOAD_BACK_STYLE)


def has_emotion_step(order: Order) -> bool:
    return not product_requires_custom_phrases(order.product) and product_emotion_count(order.product) > 0


def photos_back_payload(order: Order) -> str:
    """Back from the photo step: to emotions for the standard products, to
    the style list for the captioned pack (which has no emotion step)."""
    return PAYLOAD_BACK_EMOTIONS if has_emotion_step(order) else PAYLOAD_BACK_STYLE


def photos_screen(order: Order):
    return PHOTO_GUIDANCE, nav_rows(photos_back_payload(order))


def photo_saved_screen(order: Order):
    return PHOTO_SAVED, [[(PHOTOS_DONE_LABEL, PAYLOAD_PHOTOS_DONE)]] + nav_rows(photos_back_payload(order))


def phrases_prompt(order: Order) -> str:
    required = product_emotion_count(order.product)
    if required == 9:
        return PHRASES_PROMPT
    return PHRASES_PROMPT.replace("9 фраз", f"{required} фраз").replace("9 строк", f"{required} строк")


def phrases_screen(order: Order):
    return phrases_prompt(order), nav_rows(PAYLOAD_BACK_PHOTOS)


def contact_back_payload(order: Order) -> str:
    return PAYLOAD_BACK_PHRASES if product_requires_custom_phrases(order.product) else PAYLOAD_BACK_PHOTOS


def contact_screen(order: Order):
    return CONTACT_PROMPT, nav_rows(contact_back_payload(order))


def order_card_text(order: Order, summary: dict, *, telegram: bool) -> str:
    lines = ["🧾 Ваш заказ", f"Вариант: {product_title(order.product)}", f"Стиль: {summary['style_name']}"]
    if summary.get("quantity"):
        lines.append(f"Стикеров: {summary['quantity']}")
    lines.append(f"Фото: {order.photos.count()}")
    if product_requires_custom_phrases(order.product):
        phrases = order_custom_phrases(order)
        if phrases:
            lines.append("Надписи:")
            lines.extend(f"{index}. {phrase}" for index, phrase in enumerate(phrases, start=1))
    elif summary.get("emotions"):
        lines.append(f"Эмоции: {', '.join(summary['emotions'])}")
    if summary.get("contact"):
        lines.append(f"Контакт: {summary['contact']}")
    lines.append(f"Цена: {product_price_label(order.product, telegram=telegram)}")
    lines.append("")
    lines.append("Проверьте данные и нажмите «✅ Подтвердить заказ».")
    return "\n".join(lines)


def order_card_screen(order: Order, summary: dict, *, telegram: bool):
    rows = [[(CONFIRM_ORDER_LABEL, PAYLOAD_CONFIRM_ORDER)]]
    return order_card_text(order, summary, telegram=telegram), rows + nav_rows(PAYLOAD_BACK_CONTACT)


# --- step engine ---------------------------------------------------------------------


class OrderStepper:
    """Channel-agnostic handling of the order steps.

    ``send(text, rows)`` renders one message in the channel (rows are
    ``[(label, payload)]``); ``adapter`` is the channel adapter over
    ``ChannelOrderFlowService``. The bots keep their own transport, payment
    and preview-feedback handling and delegate everything between the main
    menu and «✅ Подтвердить заказ» here.
    """

    def __init__(self, *, adapter, send, telegram: bool):
        self.adapter = adapter
        self.send = send
        self.telegram = telegram

    # -- screens ----------------------------------------------------------

    def show(self, screen) -> None:
        text, rows = screen
        self.send(text, rows)

    def main_menu(self, identity) -> None:
        # Menu never touches a paid / in-production order; it only stops
        # waiting for free text on the in-progress one.
        self.adapter.clear_awaiting_input(identity)
        self.show(main_menu_screen())

    def products(self, identity) -> None:
        self.adapter.clear_awaiting_input(identity)
        self.show(products_screen(self.adapter.active_products(), telegram=self.telegram))

    def styles(self, product_code: str) -> None:
        self.show(styles_screen(product_code, self.adapter.active_styles()))

    def after_choice(self, order: Order) -> None:
        if has_emotion_step(order):
            self.emotions(order)
        else:
            self.show(photos_screen(order))

    def emotions(self, order: Order) -> None:
        options = self.adapter.emotion_options(product=order.product)
        required = self.adapter.required_emotion_count(product=order.product)
        self.show(emotions_screen(options, required))

    def photos(self, identity) -> Order:
        order = self.adapter.clear_awaiting_input(identity) or self.adapter.current_photo_order(identity)
        self.show(photos_screen(order))
        return order

    def photo_saved(self, identity) -> None:
        self.show(photo_saved_screen(self.adapter.current_photo_order(identity)))

    def phrases(self, identity) -> Order:
        order = self.adapter.set_awaiting_input(identity=identity, value=AWAITING_PHRASES)
        self.show(phrases_screen(order))
        return order

    def contact(self, identity) -> Order:
        order = self.adapter.set_awaiting_input(identity=identity, value=AWAITING_CONTACT)
        self.show(contact_screen(order))
        return order

    def card(self, identity) -> Order:
        order = self.adapter.clear_awaiting_input(identity) or self.adapter.current_photo_order(identity)
        self.show(order_card_screen(order, self.adapter.order_summary(order), telegram=self.telegram))
        return order

    # -- events -------------------------------------------------------------

    def handle_callback(self, identity, payload: str) -> bool:
        """Route a menu / navigation / step button. Returns False when the
        payload belongs to the channel (order confirm, consent, payment,
        preview feedback)."""
        if payload == PAYLOAD_MENU:
            self.main_menu(identity)
        elif payload == PAYLOAD_ORDER:
            self.products(identity)
        elif payload == "menu:prices":
            self.send(prices_text(self.adapter.active_products(), telegram=self.telegram), nav_rows())
        elif payload == "menu:examples":
            self.send(EXAMPLES_TEXT, nav_rows())
        elif payload == "menu:photos":
            self.send(PHOTO_REQUIREMENTS, nav_rows())
        elif payload == "menu:how":
            self.send(ORDER_HOW_IT_WORKS, nav_rows())
        elif payload == "menu:contact":
            self.send(CONTACT_TEXT, nav_rows())
        elif payload.startswith("product:"):
            product_code = payload.split(":", 1)[1]
            if not self.adapter.active_products().filter(code=product_code).exists():
                raise ChannelFlowError("Product is unavailable")
            self.styles(product_code)
        elif payload.startswith("style:"):
            _, product_code, style_code = payload.split(":", 2)
            order = self.adapter.change_order_choice(identity=identity, product_code=product_code, style_code=style_code)
            self.after_choice(order)
        elif payload.startswith("emotion:"):
            order = self.adapter.select_emotion(identity=identity, emotion_code=payload.split(":", 1)[1])
            self.show(photos_screen(order))
        elif payload == "emotions:confirm":
            order = self.adapter.confirm_emotions(identity=identity)
            self.show(photos_screen(order))
        elif payload == PAYLOAD_BACK_STYLE:
            order = self.adapter.clear_awaiting_input(identity)
            if order is None:
                self.products(identity)
            else:
                self.styles(order.product.code)
        elif payload == PAYLOAD_BACK_EMOTIONS:
            order = self.adapter.clear_awaiting_input(identity) or self.adapter.current_photo_order(identity)
            self.emotions(order)
        elif payload == PAYLOAD_BACK_PHOTOS:
            self.photos(identity)
        elif payload == PAYLOAD_BACK_PHRASES:
            self.phrases(identity)
        elif payload == PAYLOAD_BACK_CONTACT:
            self.contact(identity)
        elif payload == PAYLOAD_PHOTOS_DONE:
            self.photos_done(identity)
        else:
            return False
        return True

    def photos_done(self, identity) -> Order:
        order = self.adapter.photos_ready(identity)
        if order.photos.count() == 1:
            self.send(SINGLE_PHOTO_REMINDER, [])
        if product_requires_custom_phrases(order.product):
            self.phrases(identity)
        else:
            self.contact(identity)
        return order

    def handle_text(self, identity, text: str) -> bool:
        """Free text while the bot waits for phrases or the contact."""
        text = (text or "").strip()
        if not text:
            return False
        order = self.adapter.flow.current_photo_order_or_none(identity)
        if order is None:
            return False
        awaiting = str((order.selection or {}).get("awaiting_input") or "")
        if awaiting == AWAITING_PHRASES:
            try:
                self.adapter.save_custom_phrases(identity=identity, text=text)
            except ChannelFlowError as exc:
                received = len([line for line in text.splitlines() if line.strip()])
                self.send(customer_hint(exc, received_lines=received), nav_rows(PAYLOAD_BACK_PHOTOS))
                return True
            self.contact(identity)
            return True
        if awaiting == AWAITING_CONTACT:
            try:
                self.adapter.save_customer_contact(identity=identity, text=text)
            except ChannelFlowError as exc:
                self.send(customer_hint(exc), nav_rows(contact_back_payload(order)))
                return True
            self.card(identity)
            return True
        return False

    def confirm_order(self, identity) -> Order:
        """«✅ Подтвердить заказ»: every step is complete → the channel shows
        its consent screen next (unchanged pilot contract)."""
        return self.adapter.order_ready_to_confirm(identity)
