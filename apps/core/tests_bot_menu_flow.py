"""Bot menu + custom (captioned) stickers (owner task 2026-09-18), both channels.

Covers the owner checklist: all three products reach the order card and the
payment step (MAX: checkout URL; Telegram: send_invoice 736/460/100); the
nine-phrase path (8 lines → hint, 9 → ok); the no-phrase path; «⬅️ Назад» /
«🏠 Главное меню» from every step; the card content; the six menu items;
prices from Product.config.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.bot_menu import (
    CONTACT_PROMPT,
    MAIN_MENU,
    PHOTO_SAVED,
    PHOTO_REQUIREMENTS,
    RESULT_REQUIREMENTS,
    STYLES_TITLE,
    order_card_text,
    prices_text,
    product_title,
)
from apps.core.customer_hints import PHOTO_GUIDANCE, phrases_count_hint
from apps.core.models import Order, Payment, Product, Style
from apps.core.services.channel_order_flow import PILOT_CONSENT_TEXT
from apps.max_bot.payments import CheckoutSession
from apps.core.tests_photo_gate import good_photo_bytes


def _download_by_extension(url_or_path):
    """Fake CDN / Bot API download: PNG bytes for a .png name, JPEG otherwise
    (DRF-2164: the gate records the decoded container as the mime type)."""
    return good_photo_bytes("PNG") if str(url_or_path).lower().endswith(".png") else good_photo_bytes()

EMOTIONS = [{"code": c, "label": l} for c, l in (("hello", "Привет"), ("bye", "Пока"), ("thanks", "Спасибо"))]
NINE = [{"code": f"custom-{n}", "label": f"Фраза {n}"} for n in range(1, 10)]
PRODUCTS = {
    "sticker-pack-9-custom": ("9 стикеров с надписями", {
        "kind": "custom_pack", "quantity": 9, "emotion_count": 9, "emotions": NINE,
        "requires_custom_phrases": True, "requires_customer_contact": True,
        "price_minor": 80000, "price_stars": 736, "currency": "RUB",
    }),
    "sticker-pack-9": ("9 стикеров без надписей", {
        "kind": "pack", "quantity": 9, "emotion_count": 3, "emotions": EMOTIONS,
        "requires_customer_contact": True, "price_minor": 50000, "price_stars": 460, "currency": "RUB",
    }),
    "single-sticker": ("1 стикер", {
        "kind": "single", "quantity": 1, "emotion_count": 1, "emotions": EMOTIONS,
        "requires_customer_contact": True, "price_minor": 10000, "price_stars": 100, "currency": "RUB",
    }),
}
PHRASES_9 = "\n".join(f"Фраза номер {n}" for n in range(1, 10))
PHRASES_8 = "\n".join(f"Фраза номер {n}" for n in range(1, 9))
CONTACT = "Анна, @anna"


class FakeCheckoutProvider:
    name = "fake-external"

    def __init__(self):
        self.calls = []

    def create_checkout(self, *, payment):
        self.calls.append(payment.pk)
        return CheckoutSession(checkout_url=f"https://pay.example.test/{payment.pk}", provider_reference="ref")

    def parse_webhook(self, *, body, signature):  # pragma: no cover
        raise NotImplementedError


class CatalogMixin:
    def seed_catalog(self):
        for code, (name, config) in PRODUCTS.items():
            Product.objects.create(code=code, name=name, config=config)
        for code, name in (("3d", "3D"), ("drawn", "Рисованные"), ("meme", "Мемные"),
                           ("embroidery", "Вышивка"), ("help-choose", "Помогите выбрать")):
            Style.objects.create(code=code, name=name)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)

    def order(self):
        return Order.objects.order_by("-id").first()


# ------------------------------------------------------------------- shared texts


class BotMenuTextTests(CatalogMixin, TestCase):
    def setUp(self):
        self.seed_catalog()

    def test_prices_come_from_product_config(self):
        products = Product.objects.filter(is_active=True).order_by("id")
        rub = prices_text(products, telegram=False)
        stars = prices_text(products, telegram=True)
        self.assertEqual(rub, "💰 Цены\n\n9 стикеров с надписями — 800 ₽\n9 стикеров без надписей — 500 ₽\n1 стикер — 100 ₽")
        self.assertEqual(stars, "💰 Цены\n\n9 стикеров с надписями — 736 Stars\n9 стикеров без надписей — 460 Stars\n1 стикер — 100 Stars")
        Product.objects.filter(code="single-sticker").update(config={**PRODUCTS["single-sticker"][1], "price_minor": 15000})
        self.assertIn("1 стикер — 150 ₽", prices_text(Product.objects.order_by("id"), telegram=False))

    def test_product_title_strips_a_trailing_price(self):
        product = Product(code="x", name="9 стикеров с надписями — 800 ₽")
        self.assertEqual(product_title(product), "9 стикеров с надписями")

    def test_photo_requirements_combine_photo_guidance_and_result_requirements(self):
        self.assertIn(PHOTO_GUIDANCE, PHOTO_REQUIREMENTS)
        self.assertIn(RESULT_REQUIREMENTS, PHOTO_REQUIREMENTS)
        for phrase in ("512 px", "прозрачный фон", "512 КБ", "белая обводка", "PNG или WEBP"):
            self.assertIn(phrase, RESULT_REQUIREMENTS)


# ----------------------------------------------------------------------- drivers


class MaxBot(CatalogMixin):
    USER = {"user_id": 8101, "first_name": "Ivan"}
    CHAT_ID = 8201
    telegram = False

    def setUp(self):
        self.seed_catalog()
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)
        self.provider = FakeCheckoutProvider()
        patches = [
            mock.patch("apps.max_bot.views.MaxBotClient"),
            # the CDN serves what the file name says: a .png «sent as a file» is a
            # real PNG, a compressed .jpg photo a JPEG (the gate decodes the container)
            mock.patch("apps.max_bot.views.download_photo", side_effect=_download_by_extension),
            mock.patch("apps.max_bot.checkout.YooKassaPaymentProvider.from_env", return_value=self.provider),
        ]
        self.client_cls = patches[0].start()
        self.bot = self.client_cls.return_value
        for patch_ in patches[1:]:
            patch_.start()
        for patch_ in patches:
            self.addCleanup(patch_.stop)

    def _post(self, payload):
        return self.client.post("/max/webhook/", data=json.dumps(payload), content_type="application/json")

    def start(self):
        return self._post({"update_type": "bot_started", "chat_id": self.CHAT_ID, "user": self.USER})

    def tap(self, payload):
        return self._post({"update_type": "message_callback",
                           "callback": {"callback_id": "cb", "payload": payload, "user": self.USER},
                           "message": {"recipient": {"chat_id": self.CHAT_ID}, "body": {"mid": "m"}}})

    def say(self, text):
        return self._post({"update_type": "message_created",
                           "message": {"sender": self.USER, "recipient": {"chat_id": self.CHAT_ID},
                                       "body": {"mid": "mid-text", "text": text}}})

    def photo(self, name="p"):
        return self._post({"update_type": "message_created",
                           "message": {"sender": self.USER, "recipient": {"chat_id": self.CHAT_ID},
                                       "body": {"mid": f"mid-{name}", "attachments": [
                                           {"type": "image", "payload": {"url": f"https://cdn.max.test/{name}.jpg"}}]}}})

    def attachment(self, attachment, name="att"):
        """Any non-image attachment (file, sticker, …) as the customer sends it."""
        return self._post({"update_type": "message_created",
                           "message": {"sender": self.USER, "recipient": {"chat_id": self.CHAT_ID},
                                       "body": {"mid": f"mid-{name}", "attachments": [attachment]}}})

    def photo_as_file(self, name="p"):
        return self.attachment({"type": "file", "payload": {"url": f"https://cdn.max.test/{name}.png",
                                                            "token": "t", "filename": f"{name}.png"}}, name=name)

    def sticker(self):
        return self.attachment({"type": "sticker", "payload": {"url": "https://cdn.max.test/s.webp", "code": "x"}})

    def keyboard_drops(self):
        return [call.kwargs for call in self.bot.edit_message.call_args_list]

    def pressed_message_id(self):
        return "m"

    def fail_keyboard_drop(self):
        from apps.max_bot.client import MaxAPIError
        self.bot.edit_message.side_effect = MaxAPIError(400, "message.not.found")

    def last(self):
        return self.bot.send_message.call_args.kwargs

    def last_text(self):
        return self.last()["text"]

    def last_payloads(self):
        return [button["payload"] for row in self.last().get("buttons") or [] for button in row]

    def texts(self):
        return [call.kwargs.get("text") for call in self.bot.send_message.call_args_list]

    def payment_started(self):
        return any(
            (call.kwargs.get("buttons") or [[{}]])[0][0].get("url", "").startswith("https://pay.example.test/")
            for call in self.bot.send_message.call_args_list
        )


class TelegramBot(CatalogMixin):
    USER = {"id": 8301, "first_name": "Olga"}
    CHAT_ID = 8401
    telegram = True

    def setUp(self):
        self.seed_catalog()
        patcher = mock.patch("apps.telegram_bot.views.TelegramBotClient")
        self.client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = self.client_cls.return_value
        # a compressed photo is a .jpg; a document «sent as a file» keeps its
        # own .png path — and the bytes match the extension (the gate decodes)
        self.bot.get_file.side_effect = lambda file_id: {
            "file_path": f"documents/{file_id}.png" if file_id in self.document_ids else f"photos/{file_id}.jpg"
        }
        self.bot.download_file.side_effect = _download_by_extension
        self.document_ids = set()

    def _post(self, payload):
        return self.client.post("/telegram/webhook/", data=json.dumps(payload), content_type="application/json")

    def start(self):
        return self._post({"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, "text": "/start"}})

    def tap(self, payload):
        return self._post({"callback_query": {"id": "cb", "from": self.USER,
                                              "message": {"message_id": 777, "chat": {"id": self.CHAT_ID}}, "data": payload}})

    def say(self, text):
        return self._post({"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, "text": text}})

    def photo(self, name="p"):
        return self._post({"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, "photo": [{"file_id": name}]}})

    def attachment(self, fields):
        return self._post({"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, **fields}})

    def photo_as_file(self, name="p"):
        """A photo sent «as a file» (uncompressed): Telegram delivers a document with an image mime type."""
        self.document_ids.add(name)
        return self.attachment({"document": {"file_id": name, "file_name": f"{name}.png", "mime_type": "image/png"}})

    def sticker(self):
        return self.attachment({"sticker": {"file_id": "stk", "emoji": "😀"}})

    def keyboard_drops(self):
        return [call.kwargs for call in self.bot.edit_message_reply_markup.call_args_list]

    def pressed_message_id(self):
        return 777

    def fail_keyboard_drop(self):
        from apps.telegram_bot.client import TelegramAPIError
        self.bot.edit_message_reply_markup.side_effect = TelegramAPIError("editMessageReplyMarkup", status_code=400, description="message can't be edited")

    def last(self):
        return self.bot.send_message.call_args.kwargs

    def last_text(self):
        return self.last()["text"]

    def last_payloads(self):
        markup = self.last().get("reply_markup") or {}
        return [button["callback_data"] for row in markup.get("inline_keyboard") or [] for button in row]

    def texts(self):
        return [call.kwargs.get("text") for call in self.bot.send_message.call_args_list]

    def payment_started(self):
        return self.bot.send_invoice.called


# --------------------------------------------------------------------- scenarios


class MenuFlowScenarios:
    """Shared scenarios; run once per channel through the driver mixins."""

    def go_to_photos(self, product_code, style="3d"):
        self.start()
        self.tap("menu:order")
        self.tap(f"product:{product_code}")
        self.tap(f"style:{product_code}:{style}")
        if product_code == "sticker-pack-9":
            self.tap("emotions:confirm")
        elif product_code == "single-sticker":
            self.tap("emotion:bye")
        self.assertEqual(self.last_text(), PHOTO_GUIDANCE)

    def upload_and_finish(self, product_code, phrases=PHRASES_9, contact=CONTACT):
        self.photo("a")
        self.photo("b")
        self.tap("photos_done")
        if product_code == "sticker-pack-9-custom":
            self.say(phrases)
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.say(contact)

    def to_payment(self):
        self.tap("order:confirm")
        self.assertEqual(self.last_text(), PILOT_CONSENT_TEXT)
        self.tap("consent:accept")
        if self.telegram:
            self.tap("pay")

    # -- menu --------------------------------------------------------------

    def test_main_menu_has_six_items_and_each_answers(self):
        self.start()
        self.assertEqual(self.last_text(), MAIN_MENU)
        self.assertEqual(self.last_payloads(), ["menu:order", "menu:prices", "menu:examples", "menu:photos", "menu:how", "menu:contact", "menu:where"])
        for payload, needle in (("menu:prices", "💰 Цены"), ("menu:examples", "🖼 Примеры"),
                                ("menu:photos", "📸 Требования к фото"), ("menu:how", "❓ Как проходит заказ"),
                                ("menu:contact", "💬 Связаться со мной")):
            self.tap(payload)
            self.assertIn(needle, self.last_text())
            self.assertEqual(self.last_payloads(), ["menu:main"])
        self.tap("menu:prices")
        self.assertIn("9 стикеров с надписями — " + ("736 Stars" if self.telegram else "800 ₽"), self.last_text())
        self.tap("menu:order")
        self.assertEqual(self.last_payloads()[:3], ["product:sticker-pack-9-custom", "product:sticker-pack-9", "product:single-sticker"])
        self.assertEqual(self.last_payloads()[-1], "menu:main")

    # -- three products to the card and to payment ---------------------------

    def _run_product(self, product_code, price_needle):
        self.go_to_photos(product_code)
        self.upload_and_finish(product_code)
        card = self.last_text()
        self.assertIn("🧾 Ваш заказ", card)
        self.assertIn(f"Вариант: {PRODUCTS[product_code][0]}", card)
        self.assertIn("Стиль: 3D", card)
        self.assertIn("Фото: 2", card)
        self.assertIn(f"Контакт: {CONTACT}", card)
        self.assertIn(price_needle, card)
        self.assertEqual(self.last_payloads(), ["order:confirm", "back:contact", "menu:main"])
        order = self.order()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        self.assertEqual(order.selection.get("contact"), CONTACT)
        self.to_payment()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertTrue(order.consent_accepted)
        self.assertTrue(self.payment_started())
        payment = Payment.objects.get(order=order)
        return order, payment

    def test_custom_pack_reaches_payment_with_nine_phrases(self):
        order, payment = self._run_product("sticker-pack-9-custom", "736 Stars" if self.telegram else "800 ₽")
        self.assertEqual(order.selection["custom_phrases"], PHRASES_9.split("\n"))
        self.assertEqual(order.selection["emotions"], [f"custom-{n}" for n in range(1, 10)])
        card = [t for t in self.texts() if t and t.startswith("🧾")][-1]
        self.assertIn("Надписи:", card)
        self.assertIn("1. Фраза номер 1", card)
        self.assertIn("9. Фраза номер 9", card)
        self.assertEqual(payment.amount_minor, 736 if self.telegram else 80000)
        if self.telegram:
            self.assertEqual(self.bot.send_invoice.call_args.kwargs["amount_stars"], 736)

    def test_pack_reaches_payment(self):
        order, payment = self._run_product("sticker-pack-9", "460 Stars" if self.telegram else "500 ₽")
        self.assertNotIn("custom_phrases", order.selection)
        self.assertEqual(payment.amount_minor, 460 if self.telegram else 50000)
        if self.telegram:
            self.assertEqual(self.bot.send_invoice.call_args.kwargs["amount_stars"], 460)

    def test_single_reaches_payment(self):
        order, payment = self._run_product("single-sticker", "100 Stars" if self.telegram else "100 ₽")
        self.assertEqual(order.selection["emotions"], ["bye"])
        self.assertEqual(payment.amount_minor, 100 if self.telegram else 10000)
        if self.telegram:
            self.assertEqual(self.bot.send_invoice.call_args.kwargs["amount_stars"], 100)

    # -- phrases ---------------------------------------------------------------

    def test_eight_lines_are_rejected_with_a_hint_then_nine_accepted(self):
        self.go_to_photos("sticker-pack-9-custom")
        self.photo("a")
        self.tap("photos_done")
        self.assertIn("9 фраз", self.last_text())
        self.assertEqual(self.last_payloads(), ["back:photos", "menu:main"])
        self.say(PHRASES_8)
        self.assertEqual(self.last_text(), phrases_count_hint(9, 8))
        self.assertEqual(self.order().selection.get("awaiting_input"), "phrases")
        self.assertNotIn("custom_phrases", self.order().selection)
        self.say(PHRASES_9)
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.assertEqual(self.order().selection["custom_phrases"], PHRASES_9.split("\n"))

    def test_invalid_contact_is_rejected_with_a_hint(self):
        self.go_to_photos("single-sticker")
        self.photo("a")
        self.tap("photos_done")
        self.say("ab")
        self.assertIn("не короче 3 символов", self.last_text())
        self.assertEqual(self.order().selection.get("awaiting_input"), "contact")
        self.say(CONTACT)
        self.assertIn("🧾 Ваш заказ", self.last_text())

    # -- optional contact (owner GO 2026-09-20) -------------------------------------

    def test_contact_can_be_skipped_and_the_order_reaches_payment(self):
        self.go_to_photos("single-sticker")
        self.photo("a")
        self.tap("photos_done")
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.assertEqual(self.last_payloads(), ["contact:skip", "back:photos", "menu:main"])
        self.tap("contact:skip")
        card = self.last_text()
        self.assertIn("🧾 Ваш заказ", card)
        self.assertIn("Связь: в этом чате", card)
        self.assertNotIn("Контакт:", card)
        order = self.order()
        self.assertTrue(order.selection.get("contact_skipped"))
        self.assertNotIn("contact", order.selection)
        self.assertNotIn("awaiting_input", order.selection)
        self.to_payment()
        summary = [t for t in self.texts() if t and t.startswith("Ваш заказ:")][-1]
        self.assertIn("Связь: в этом чате", summary)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertTrue(order.consent_accepted)
        self.assertTrue(self.payment_started())

    def test_contact_text_still_accepted_and_replaces_a_skip(self):
        self.go_to_photos("sticker-pack-9")
        self.photo("a")
        self.tap("photos_done")
        self.tap("contact:skip")
        self.assertIn("Связь: в этом чате", self.last_text())
        self.tap("back:contact")
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.say("ab")  # still validated
        self.assertIn("не короче 3 символов", self.last_text())
        self.say(CONTACT)
        card = self.last_text()
        self.assertIn(f"Контакт: {CONTACT}", card)
        self.assertNotIn("Связь: в этом чате", card)
        order = self.order()
        self.assertEqual(order.selection.get("contact"), CONTACT)
        self.assertNotIn("contact_skipped", order.selection)

    def test_confirm_without_contact_is_refused(self):
        self.go_to_photos("single-sticker")
        self.photo("a")
        self.tap("photos_done")
        response = self.tap("order:confirm")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Оставьте имя и удобный способ связи", self.last_text())
        self.assertFalse(self.order().consent_accepted)

    # -- back / menu from every step ---------------------------------------------

    def test_back_from_style_returns_to_products(self):
        self.start()
        self.tap("menu:order")
        self.tap("product:sticker-pack-9")
        self.assertEqual(self.last_text(), STYLES_TITLE)
        self.assertEqual(self.last_payloads()[-2:], ["menu:order", "menu:main"])
        self.tap("menu:order")
        self.assertEqual(self.last_payloads()[0], "product:sticker-pack-9-custom")
        self.assertIsNone(self.order())

    def test_back_from_emotions_returns_to_styles(self):
        self.start()
        self.tap("menu:order")
        self.tap("product:single-sticker")
        self.tap("style:single-sticker:3d")
        self.assertEqual(self.last_payloads()[-2:], ["back:style", "menu:main"])
        self.tap("back:style")
        self.assertEqual(self.last_text(), STYLES_TITLE)
        self.tap("style:single-sticker:meme")
        self.assertEqual(self.order().style.code, "meme")
        self.assertEqual(Order.objects.count(), 1)

    def test_back_from_photos_keeps_order_and_photos(self):
        self.go_to_photos("sticker-pack-9")
        self.photo("a")
        self.assertEqual(self.last_payloads(), ["photos_done", "back:emotions", "menu:main"])
        self.tap("back:emotions")
        self.assertIn("В набор входят", self.last_text())
        self.tap("back:style")
        self.tap("style:sticker-pack-9:drawn")
        order = self.order()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        self.assertEqual(order.photos.count(), 1, "photos survive Back")
        self.assertEqual(order.style.code, "drawn")
        self.assertEqual(Order.objects.count(), 1)

    def test_back_from_photos_custom_goes_to_styles(self):
        self.go_to_photos("sticker-pack-9-custom")
        self.assertEqual(self.last_payloads(), ["back:style", "menu:main"])
        self.tap("back:style")
        self.assertEqual(self.last_text(), STYLES_TITLE)

    def test_back_from_phrases_and_contact(self):
        self.go_to_photos("sticker-pack-9-custom")
        self.photo("a")
        self.tap("photos_done")
        self.tap("back:photos")
        self.assertEqual(self.last_text(), PHOTO_SAVED)  # a photo is already in → «Фото загружены» is offered
        self.assertNotIn("awaiting_input", self.order().selection)
        self.tap("photos_done")
        self.say(PHRASES_9)
        self.assertEqual(self.last_payloads(), ["contact:skip", "back:phrases", "menu:main"])
        self.tap("back:phrases")
        self.assertIn("9 фраз", self.last_text())
        self.assertEqual(self.order().selection.get("awaiting_input"), "phrases")
        self.say(PHRASES_9)
        self.say(CONTACT)
        self.tap("back:contact")
        self.assertEqual(self.last_text(), CONTACT_PROMPT)
        self.assertEqual(self.order().selection.get("contact"), CONTACT, "previous contact kept")
        self.say("Пётр, +7 900 000-00-00")
        self.assertIn("Контакт: Пётр, +7 900 000-00-00", self.last_text())

    def test_main_menu_clears_awaiting_input_and_keeps_the_order(self):
        self.go_to_photos("single-sticker")
        self.photo("a")
        self.tap("photos_done")
        self.assertEqual(self.order().selection.get("awaiting_input"), "contact")
        self.tap("menu:main")
        self.assertEqual(self.last_text(), MAIN_MENU)
        order = self.order()
        self.assertNotIn("awaiting_input", order.selection)
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        # free text after Menu is not swallowed as a contact
        self.say("привет")
        self.assertNotIn("contact", order.selection)

    def test_product_change_resets_selection_but_keeps_photos_and_contact(self):
        self.go_to_photos("single-sticker")
        self.photo("a")
        self.tap("photos_done")
        self.say(CONTACT)
        self.tap("menu:order")
        self.tap("product:sticker-pack-9-custom")
        self.tap("style:sticker-pack-9-custom:3d")
        order = self.order()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(order.product.code, "sticker-pack-9-custom")
        self.assertEqual(order.selection.get("emotions"), [])
        self.assertEqual(order.selection.get("contact"), CONTACT)
        self.assertEqual(order.photos.count(), 1)
        self.assertEqual(self.last_text(), PHOTO_SAVED)  # the kept photo counts: «Фото загружены» is offered

    def test_menu_after_payment_does_not_touch_the_paid_order(self):
        self.go_to_photos("single-sticker")
        self.upload_and_finish("single-sticker")
        self.to_payment()
        order = self.order()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.tap("menu:main")
        self.tap("menu:order")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(Order.objects.count(), 1)

    def test_order_card_text_lists_phrases(self):
        self.go_to_photos("sticker-pack-9-custom")
        self.upload_and_finish("sticker-pack-9-custom")
        order = self.order()
        from apps.core.services.channel_order_flow import ChannelOrderFlowService

        text = order_card_text(order, ChannelOrderFlowService().order_summary(order), telegram=self.telegram)
        self.assertEqual(text, self.last_text())


class MaxMenuFlowTests(MenuFlowScenarios, MaxBot, TestCase):
    pass


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramMenuFlowTests(MenuFlowScenarios, TelegramBot, TestCase):
    pass
