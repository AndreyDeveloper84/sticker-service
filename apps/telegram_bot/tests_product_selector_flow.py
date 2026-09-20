"""DRF-2050 acceptance: Telegram pack/single flows to READY_FOR_CHECKOUT.

start → product → style → emotion(s) → photos → summary → pay invoice,
through the real webhook view with the Telegram client mocked at the
boundary. Prices must come from Product.config (price_stars / XTR).
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.bot_menu import CONTACT_PROMPT
from apps.core.customer_hints import EMOTION_CHOICE_HINT, NEED_PHOTO_HINT
from apps.core.models import Order, Product, Style
from apps.core.services.channel_order_flow import order_emotion_codes

WEBHOOK_URL = "/telegram/webhook/"

EMOTIONS = [
    {"code": "hello", "label": "Привет"},
    {"code": "bye", "label": "Пока"},
    {"code": "thanks", "label": "Спасибо"},
]

PACK_CONFIG = {
    "kind": "pack",
    "quantity": 9,
    "emotion_count": 3,
    "emotions": EMOTIONS,
    "price_minor": 50000,
    "price_stars": 500,
    "currency": "RUB",
}

SINGLE_CONFIG = {
    "kind": "single",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": EMOTIONS,
    "price_minor": 10000,
    "price_stars": 100,
    "currency": "RUB",
}

USER = {"id": 3101, "first_name": "Anna", "username": "anna_tg"}
CHAT_ID = 4101


def _message(text):
    return {"message": {"from": USER, "chat": {"id": CHAT_ID}, "text": text}}


def _photo_message():
    return {
        "message": {
            "from": USER,
            "chat": {"id": CHAT_ID},
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
        }
    }


def _callback(data, callback_id="cb-1"):
    return {
        "callback_query": {
            "id": callback_id,
            "from": USER,
            "message": {"chat": {"id": CHAT_ID}},
            "data": data,
        }
    }


class TelegramProductSelectorFlowTests(TestCase):
    def setUp(self):
        Product.objects.create(code="sticker-pack-9", name="Стикерпак — 9 стикеров", config=PACK_CONFIG)
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def _start_flow(self, client, product_code):
        response = self._post(_message("/start"))
        self.assertEqual(response.status_code, 200)
        # /start → main menu (6 items); «🎨 Заказать стикеры» → products
        keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], "menu:order")
        self.assertEqual(len(keyboard), 7)  # six items + «📍 Где я?»
        response = self._post(_callback("menu:order"))
        self.assertEqual(response.status_code, 200)
        keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(
            [row[0]["callback_data"] for row in keyboard],
            ["product:sticker-pack-9", "product:single-sticker", "menu:main"],
        )

        client.reset_mock()
        response = self._post(_callback(f"product:{product_code}"))
        self.assertEqual(response.status_code, 200)
        keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], f"style:{product_code}:comic")

        client.reset_mock()
        response = self._post(_callback(f"style:{product_code}:comic"))
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        self.assertEqual(order.product.code, product_code)
        return order

    def _send_photo_and_finish(self, client, media_root):
        with override_settings(MEDIA_ROOT=Path(media_root)):
            client.reset_mock()
            client.get_file.return_value = {"file_path": "photos/source.jpg"}
            client.download_file.return_value = b"image-bytes"
            response = self._post(_photo_message())
            self.assertEqual(response.status_code, 200)
            keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(keyboard[0][0]["callback_data"], "photos_done")

            client.reset_mock()
            response = self._post(_callback("photos_done", callback_id="cb-done"))
            self.assertEqual(response.status_code, 200)

    def test_pack_flow_reaches_checkout_with_summary(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._start_flow(client, "sticker-pack-9")

            # style → deterministic emotion set with a single confirm button
            kwargs = client.send_message.call_args.kwargs
            self.assertIn("3 эмоций", kwargs["text"])
            self.assertIn("Привет", kwargs["text"])
            keyboard = kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(keyboard[0], [{"text": "Подтвердить набор", "callback_data": "emotions:confirm"}])
            self.assertEqual([row[0]["callback_data"] for row in keyboard[1:]], ["back:style", "menu:main"])

            # photos_done before the emotion step is rejected with a hint (200), no state mutation
            client.reset_mock()
            response = self._post(_callback("photos_done", callback_id="cb-early"))
            self.assertEqual(response.status_code, 200)
            # photos are checked first: no photo yet → the photo hint
            self.assertEqual(client.send_message.call_args.kwargs["text"], NEED_PHOTO_HINT)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

            client.reset_mock()
            response = self._post(_callback("emotions:confirm"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), ["hello", "bye", "thanks"])

            with tempfile.TemporaryDirectory() as media_root:
                self._send_photo_and_finish(client, media_root)

            order.refresh_from_db()
            # photos_done → contact step → order card → confirm → consent screen;
            # the invoice waits for consent:accept + pay
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            self.assertEqual(client.send_message.call_args.kwargs["text"], CONTACT_PROMPT)
            self._post(_message("Анна, @anna"))
            card = client.send_message.call_args.kwargs
            self.assertEqual(card["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "order:confirm")
            self.assertIn("Контакт: Анна, @anna", card["text"])
            self._post(_callback("order:confirm", callback_id="cb-confirm"))
            keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(keyboard, [[{"text": "Принимаю", "callback_data": "consent:accept"}]])
            client.send_invoice.assert_not_called()

            client.reset_mock()
            response = self._post(_callback("consent:accept", callback_id="cb-consent"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
            self.assertTrue(order.consent_accepted)
            text = client.send_message.call_args.kwargs["text"]
            self.assertIn("Стикерпак — 9 стикеров", text)
            self.assertIn("Стикеров: 9", text)
            self.assertIn("Привет, Пока, Спасибо", text)
            self.assertIn("500 Stars", text)
            keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(keyboard[0][0]["callback_data"], "pay")

            # pay → invoice amount comes from Product.config price_stars
            client.reset_mock()
            response = self._post(_callback("pay"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_invoice.call_args.kwargs["amount_stars"], 500)

    def test_single_flow_reaches_checkout_with_summary(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._start_flow(client, "single-sticker")

            # style → one button per emotion
            kwargs = client.send_message.call_args.kwargs
            keyboard = kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(
                [row[0]["callback_data"] for row in keyboard],
                ["emotion:hello", "emotion:bye", "emotion:thanks", "back:style", "menu:main"],
            )

            # unknown emotion is rejected with a hint (200), selection unchanged
            client.reset_mock()
            response = self._post(_callback("emotion:nope"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], EMOTION_CHOICE_HINT)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), [])

            client.reset_mock()
            response = self._post(_callback("emotion:thanks"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), ["thanks"])

            with tempfile.TemporaryDirectory() as media_root:
                self._send_photo_and_finish(client, media_root)

            order.refresh_from_db()
            # photos_done → contact step → order card → confirm → consent screen;
            # the invoice waits for consent:accept + pay
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            self.assertEqual(client.send_message.call_args.kwargs["text"], CONTACT_PROMPT)
            self._post(_message("Анна, @anna"))
            card = client.send_message.call_args.kwargs
            self.assertEqual(card["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "order:confirm")
            self.assertIn("Контакт: Анна, @anna", card["text"])
            self._post(_callback("order:confirm", callback_id="cb-confirm"))
            keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertEqual(keyboard, [[{"text": "Принимаю", "callback_data": "consent:accept"}]])
            client.send_invoice.assert_not_called()

            client.reset_mock()
            response = self._post(_callback("consent:accept", callback_id="cb-consent"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
            self.assertTrue(order.consent_accepted)
            text = client.send_message.call_args.kwargs["text"]
            self.assertIn("Один стикер", text)
            self.assertIn("Стикеров: 1", text)
            self.assertIn("Эмоции: Спасибо", text)
            self.assertIn("100 Stars", text)

            client.reset_mock()
            response = self._post(_callback("pay"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_invoice.call_args.kwargs["amount_stars"], 100)
