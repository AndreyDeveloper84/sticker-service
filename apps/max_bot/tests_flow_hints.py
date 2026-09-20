"""DRF-2083: out-of-step input on MAX is answered with a hint, not a silent 409.

Live evidence: a tester sent photos before /start; the webhook answered 409
and the bot said nothing. Now every ChannelFlowError / PreviewFeedbackError
from a step handler becomes a short Russian hint to the customer + HTTP 200
(no MAX redelivery loop), the photo is never downloaded when no order is
waiting for it, and the callback is still ACKed.
"""

import json
from unittest import mock

from django.test import TestCase

from apps.core.customer_hints import (
    CONTINUE_ORDER_HINT,
    GENERIC_HINT,
    NO_PREVIEW_HINT,
    PRODUCT_UNAVAILABLE_HINT,
    START_HINT,
    customer_hint,
)
from apps.core.models import Order, Product, Style
from apps.core.services.channel_order_flow import ChannelFlowError
from apps.core.services.preview_feedback import PreviewFeedbackError
from apps.max_bot.client import MaxAPIError
from apps.core.tests_photo_gate import good_photo_bytes

WEBHOOK_URL = "/max/webhook/"
USER = {"user_id": 7301, "first_name": "Ivan"}
CHAT_ID = 9301


def _callback(payload, callback_id="cb-1"):
    return {
        "update_type": "message_callback",
        "callback": {"callback_id": callback_id, "payload": payload, "user": USER},
        "message": {"recipient": {"chat_id": CHAT_ID}, "body": {"mid": "mid-1"}},
    }


def _photo(url="https://cdn.max.test/p.jpg"):
    return {
        "update_type": "message_created",
        "message": {
            "sender": USER,
            "recipient": {"chat_id": CHAT_ID},
            "body": {"mid": "mid-photo", "attachments": [{"type": "image", "payload": {"url": url}}]},
        },
    }


class MaxFlowHintTests(TestCase):
    def setUp(self):
        Product.objects.create(code="stickers", name="Sticker Pack")
        Style.objects.create(code="classic", name="Classic")
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def test_photo_without_order_gets_start_hint_and_is_not_downloaded(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.download_photo"
        ) as download:
            client = client_cls.return_value

            response = self._post(_photo())

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ok"], True)
            download.assert_not_called()
            client.send_message.assert_called_once()
            kwargs = client.send_message.call_args.kwargs
            self.assertEqual(kwargs["chat_id"], str(CHAT_ID))
            self.assertEqual(kwargs["text"], START_HINT)
            self.assertFalse(Order.objects.exists())

    def test_stale_callback_gets_hint_and_is_acked(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("photos_done", "cb-stale"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], START_HINT)
            client.answer_callback.assert_called_once_with(callback_id="cb-stale")

    def test_preview_feedback_without_preview_gets_hint(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("preview_approve"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], NO_PREVIEW_HINT)

    def test_unavailable_product_gets_hint(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value

            response = self._post(_callback("product:nope"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], PRODUCT_UNAVAILABLE_HINT)

    def test_hint_send_failure_still_answers_200(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value
            client.send_message.side_effect = MaxAPIError(502, "max down")

            with self.assertLogs("apps.max_bot.views", level="WARNING"):
                response = self._post(_photo())

            self.assertEqual(response.status_code, 200)

    def test_happy_path_still_saves_photo(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.download_photo", return_value=good_photo_bytes()
        ) as download:
            client = client_cls.return_value
            self._post(_callback("product:stickers"))
            self._post(_callback("style:stickers:classic"))
            client.reset_mock()

            with self.settings(MEDIA_ROOT=self._tmp()):
                response = self._post(_photo())

            self.assertEqual(response.status_code, 200)
            download.assert_called_once()
            self.assertEqual(Order.objects.get().photos.count(), 1)
            self.assertEqual(client.send_message.call_args.kwargs["buttons"][0][0]["payload"], "photos_done")

    def _tmp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


class CustomerHintMappingTests(TestCase):
    def test_known_messages_map_to_specific_hints(self):
        self.assertEqual(customer_hint(ChannelFlowError("No order is waiting for photos")), START_HINT)
        self.assertEqual(customer_hint(ChannelFlowError("Another order is already waiting for photos")), CONTINUE_ORDER_HINT)
        self.assertEqual(customer_hint(PreviewFeedbackError("Order is not awaiting preview feedback")), NO_PREVIEW_HINT)

    def test_unknown_message_falls_back_to_generic(self):
        self.assertEqual(customer_hint(ChannelFlowError("something new")), GENERIC_HINT)
