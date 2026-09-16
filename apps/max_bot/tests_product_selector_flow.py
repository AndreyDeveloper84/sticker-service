"""DRF-2050 acceptance: MAX pack/single flows to READY_FOR_CHECKOUT.

bot_started → product → style → emotion(s) → photos → summary → checkout,
through the real webhook view with the MAX client mocked at the boundary.
Prices must come from Product.config (price_minor / RUB).
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import Order, Product, Style
from apps.core.services.channel_order_flow import order_emotion_codes

WEBHOOK_URL = "/max/webhook/"

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

USER = {"user_id": 7101, "first_name": "Boris"}
CHAT_ID = 9101


def _callback(payload, callback_id="cb-1"):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": USER,
        },
        "message": {"recipient": {"chat_id": CHAT_ID}, "body": {"mid": "mid-1"}},
    }


def _photo_message():
    return {
        "update_type": "message_created",
        "message": {
            "sender": USER,
            "recipient": {"chat_id": CHAT_ID},
            "body": {
                "mid": "mid-photo",
                "attachments": [{"type": "image", "payload": {"url": "https://cdn.max.test/p.jpg"}}],
            },
        },
    }


class MaxProductSelectorFlowTests(TestCase):
    def setUp(self):
        Product.objects.create(code="sticker-pack-9", name="Стикерпак — 9 стикеров", config=PACK_CONFIG)
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")
        # Secret enforcement is covered by the regression suite; disable here.
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def _start_flow(self, client, product_code):
        response = self._post({"update_type": "bot_started", "chat_id": CHAT_ID, "user": USER})
        self.assertEqual(response.status_code, 200)
        buttons = client.send_message.call_args.kwargs["buttons"]
        self.assertEqual(
            [row[0]["payload"] for row in buttons],
            ["product:sticker-pack-9", "product:single-sticker"],
        )

        client.reset_mock()
        response = self._post(_callback(f"product:{product_code}"))
        self.assertEqual(response.status_code, 200)
        buttons = client.send_message.call_args.kwargs["buttons"]
        self.assertEqual(buttons[0][0]["payload"], f"style:{product_code}:comic")

        client.reset_mock()
        response = self._post(_callback(f"style:{product_code}:comic"))
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        self.assertEqual(order.product.code, product_code)
        return order

    def _send_photo(self, client, media_root):
        with override_settings(MEDIA_ROOT=Path(media_root)):
            with mock.patch("apps.max_bot.views.download_photo", return_value=b"image-bytes"):
                client.reset_mock()
                response = self._post(_photo_message())
                self.assertEqual(response.status_code, 200)
                buttons = client.send_message.call_args.kwargs["buttons"]
                self.assertEqual(buttons[0][0]["payload"], "photos_done")

    def test_pack_flow_reaches_checkout_with_summary(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            order = self._start_flow(client, "sticker-pack-9")

            # style → deterministic emotion set with a single confirm button
            kwargs = client.send_message.call_args.kwargs
            self.assertIn("3 эмоций", kwargs["text"])
            self.assertIn("Привет", kwargs["text"])
            self.assertEqual(kwargs["buttons"], [[{"text": "Подтвердить набор", "payload": "emotions:confirm"}]])

            # photos_done before the emotion step is rejected with 409, no state mutation
            client.reset_mock()
            response = self._post(_callback("photos_done", callback_id="cb-early"))
            self.assertEqual(response.status_code, 409)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            checkout_mock.assert_not_called()

            client.reset_mock()
            response = self._post(_callback("emotions:confirm"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), ["hello", "bye", "thanks"])

            with tempfile.TemporaryDirectory() as media_root:
                self._send_photo(client, media_root)

            client.reset_mock()
            response = self._post(_callback("photos_done", callback_id="cb-done"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

            text = client.send_message.call_args.kwargs["text"]
            self.assertIn("Стикерпак — 9 стикеров", text)
            self.assertIn("Стикеров: 9", text)
            self.assertIn("Привет, Пока, Спасибо", text)
            self.assertIn("500 ₽", text)
            checkout_mock.assert_called_once()
            self.assertEqual(checkout_mock.call_args.kwargs["chat_id"], str(CHAT_ID))

    def test_single_flow_reaches_checkout_with_summary(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            order = self._start_flow(client, "single-sticker")

            # style → one button per emotion
            buttons = client.send_message.call_args.kwargs["buttons"]
            self.assertEqual(
                [row[0]["payload"] for row in buttons],
                ["emotion:hello", "emotion:bye", "emotion:thanks"],
            )

            # unknown emotion is rejected with 409, selection unchanged
            client.reset_mock()
            response = self._post(_callback("emotion:nope"))
            self.assertEqual(response.status_code, 409)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), [])

            client.reset_mock()
            response = self._post(_callback("emotion:bye"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order_emotion_codes(order), ["bye"])

            with tempfile.TemporaryDirectory() as media_root:
                self._send_photo(client, media_root)

            client.reset_mock()
            response = self._post(_callback("photos_done", callback_id="cb-done"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

            text = client.send_message.call_args.kwargs["text"]
            self.assertIn("Один стикер", text)
            self.assertIn("Стикеров: 1", text)
            self.assertIn("Эмоции: Пока", text)
            self.assertIn("100 ₽", text)
            checkout_mock.assert_called_once()
