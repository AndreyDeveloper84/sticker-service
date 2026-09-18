"""DRF-2083 (Telegram parity): out-of-step input is answered with a hint and
HTTP 200; a photo that no order is waiting for is never fetched; callbacks
are still answered.
"""

import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.customer_hints import NO_PREVIEW_HINT, PRODUCT_UNAVAILABLE_HINT, START_HINT
from apps.core.models import Order, Product, Style
from apps.telegram_bot.client import TelegramAPIError

WEBHOOK_URL = "/telegram/webhook/"
USER = {"id": 3301, "first_name": "Olga"}
CHAT_ID = 4301


def _callback(data, callback_id="cb-1"):
    return {"callback_query": {"id": callback_id, "from": USER, "message": {"chat": {"id": CHAT_ID}}, "data": data}}


def _photo():
    return {"message": {"from": USER, "chat": {"id": CHAT_ID}, "photo": [{"file_id": "big"}]}}


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramFlowHintTests(TestCase):
    def setUp(self):
        Product.objects.create(code="stickers", name="Sticker Pack", config={"price_stars": 100})
        Style.objects.create(code="classic", name="Classic")

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def test_photo_without_order_gets_start_hint_and_is_not_downloaded(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_photo())

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ok"], True)
            client.get_file.assert_not_called()
            client.download_file.assert_not_called()
            client.send_message.assert_called_once_with(chat_id=CHAT_ID, text=START_HINT)
            self.assertFalse(Order.objects.exists())

    def test_stale_callback_gets_hint_and_is_answered(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("photos_done", "cb-stale"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], START_HINT)
            client.answer_callback_query.assert_called_once_with(callback_query_id="cb-stale")

    def test_preview_feedback_without_preview_gets_hint(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("preview_approve"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ok"], True)
            self.assertEqual(client.send_message.call_args.kwargs["text"], NO_PREVIEW_HINT)

    def test_unavailable_product_gets_hint(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("product:nope"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], PRODUCT_UNAVAILABLE_HINT)

    def test_hint_send_failure_still_answers_200(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            client.send_message.side_effect = TelegramAPIError("sendMessage", description="network: ConnectError")

            with self.assertLogs("apps.telegram_bot.views", level="WARNING"):
                response = self._post(_photo())

            self.assertEqual(response.status_code, 200)

    def test_payment_errors_keep_their_contract(self):
        # "pay" with nothing to pay for is a TelegramPaymentError: unchanged
        # answer (200, ok=false), no hint — payment paths are not in DRF-2083.
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("pay"))

            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["ok"])
            client.send_message.assert_not_called()
