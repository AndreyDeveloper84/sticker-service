"""Regression: existing Sticker MAX flow over the new transport layer.

start → product → style → photo → photos_done, through the real webhook
view with the MAX client mocked at the boundary.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import ChannelIdentity, Order, Product, Style

WEBHOOK_URL = "/max/webhook/"


def _callback_payload(*, user_id=7001, chat_id=9001, payload, callback_id="cb-1"):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": user_id, "first_name": "Ivan"},
        },
        "message": {"recipient": {"chat_id": chat_id}, "body": {"mid": "mid-1"}},
    }


class MaxStickerFlowRegressionTests(TestCase):
    def setUp(self):
        Product.objects.create(code="stickers", name="Sticker Pack")
        Style.objects.create(code="classic", name="Classic")
        # The view enforces MAX_WEBHOOK_SECRET only when the env var is
        # non-empty; staging has it set, so neutralise it for these tests
        # (secret enforcement itself is covered by test_webhook_secret_enforced).
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, payload):
        return self.client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_start_product_style_photo_photos_done(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value

            # bot_started → products list
            response = self._post(
                {
                    "update_type": "bot_started",
                    "chat_id": 9001,
                    "user": {"user_id": 7001, "first_name": "Ivan"},
                }
            )
            self.assertEqual(response.status_code, 200)
            client.send_message.assert_called_once()
            kwargs = client.send_message.call_args.kwargs
            self.assertEqual(kwargs["chat_id"], "9001")
            self.assertEqual(kwargs["buttons"][0][0]["payload"], "menu:order")
            # «🎨 Заказать стикеры» → products list
            client.reset_mock()
            response = self._post(_callback_payload(payload="menu:order", callback_id="cb-0"))
            self.assertEqual(response.status_code, 200)
            kwargs = client.send_message.call_args.kwargs
            buttons = kwargs["buttons"]
            self.assertEqual(buttons[0][0]["payload"], "product:stickers")

            # product → styles
            client.reset_mock()
            response = self._post(_callback_payload(payload="product:stickers"))
            self.assertEqual(response.status_code, 200)
            kwargs = client.send_message.call_args.kwargs
            self.assertEqual(kwargs["buttons"][0][0]["payload"], "style:stickers:classic")
            client.answer_callback.assert_called_once_with(callback_id="cb-1")

            # style → order created
            client.reset_mock()
            response = self._post(_callback_payload(payload="style:stickers:classic"))
            self.assertEqual(response.status_code, 200)
            order = Order.objects.get()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

        # photo upload → saved (download mocked at the photo boundary)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
                    "apps.max_bot.views.download_photo", return_value=b"image-bytes"
                ):
                    client = client_cls.return_value
                    response = self._post(
                        {
                            "update_type": "message_created",
                            "message": {
                                "sender": {"user_id": 7001, "first_name": "Ivan"},
                                "recipient": {"chat_id": 9001},
                                "body": {
                                    "mid": "mid-photo",
                                    "attachments": [
                                        {"type": "image", "payload": {"url": "https://cdn.max.test/p.jpg"}}
                                    ],
                                },
                            },
                        }
                    )
                    self.assertEqual(response.status_code, 200)
                    order.refresh_from_db()
                    self.assertEqual(order.photos.count(), 1)
                    kwargs = client.send_message.call_args.kwargs
                    self.assertEqual(kwargs["buttons"][0][0]["payload"], "photos_done")

        # photos_done → contact step → card → confirm → consent screen
        # (no checkout yet, order stays in AWAITING_PHOTOS)
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            response = self._post(_callback_payload(payload="photos_done", callback_id="cb-9"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            self.assertFalse(order.consent_accepted)
            checkout_mock.assert_not_called()
            kwargs = client.send_message.call_args.kwargs
            self.assertEqual(kwargs["buttons"][-1][0]["payload"], "menu:main")
            client.answer_callback.assert_called_once_with(callback_id="cb-9")
            self._post({
                "update_type": "message_created",
                "message": {"sender": {"user_id": 7001, "first_name": "Ivan"}, "recipient": {"chat_id": 9001},
                            "body": {"mid": "mid-contact", "text": "Иван, +7 900 000-00-00"}},
            })
            self.assertEqual(client.send_message.call_args.kwargs["buttons"][0][0]["payload"], "order:confirm")
            self._post(_callback_payload(payload="order:confirm", callback_id="cb-9b"))
            self.assertEqual(client.send_message.call_args.kwargs["buttons"][0][0]["payload"], "consent:accept")

        # consent:accept → consent persisted, summary, checkout started
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            response = self._post(_callback_payload(payload="consent:accept", callback_id="cb-10"))
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
            self.assertTrue(order.consent_accepted)
            checkout_mock.assert_called_once()
            self.assertEqual(checkout_mock.call_args.kwargs["chat_id"], "9001")
            client.answer_callback.assert_called_once_with(callback_id="cb-10")

    def test_callback_ack_failure_does_not_fail_webhook(self):
        """A failed /answers ACK must not 502 the webhook: MAX would retry
        the update and the user would get the reply twice."""
        from apps.max_bot.client import MaxAPIError

        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value
            client.answer_callback.side_effect = MaxAPIError(400, "bad callback")
            response = self._post(_callback_payload(payload="product:stickers"))
            self.assertEqual(response.status_code, 200)
            client.send_message.assert_called_once()

    def test_malformed_webhook_returns_400_not_500(self):
        response = self._post({"update_type": "message_created", "message": {}})
        self.assertEqual(response.status_code, 400)

    def test_unknown_update_type_returns_400(self):
        response = self._post({"update_type": "bot_added"})
        self.assertEqual(response.status_code, 400)

    def test_webhook_secret_enforced(self):
        with mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": "s3cret"}):
            response = self._post({"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 2}})
            self.assertEqual(response.status_code, 403)
            with mock.patch("apps.max_bot.views.MaxBotClient"):
                response = self.client.post(
                    WEBHOOK_URL,
                    data=json.dumps({"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 2}}),
                    content_type="application/json",
                    HTTP_X_MAX_BOT_API_SECRET="s3cret",
                )
            self.assertEqual(response.status_code, 200)

    def test_invalid_photo_upload_replies_instead_of_500(self):
        """MediaService rejects e.g. a GIF; the customer gets a hint and MAX
        gets a 200 (a 500 would make MAX redeliver the same update)."""
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            self._post(_callback_payload(payload="product:stickers"))
            self._post(_callback_payload(payload="style:stickers:classic"))
            client = client_cls.return_value
            client.reset_mock()
            with mock.patch("apps.max_bot.views.download_photo", return_value=b"gif-bytes"):
                response = self._post(
                    {
                        "update_type": "message_created",
                        "message": {
                            "sender": {"user_id": 7001, "first_name": "Ivan"},
                            "recipient": {"chat_id": 9001},
                            "body": {
                                "mid": "mid-gif",
                                "attachments": [
                                    {"type": "image", "payload": {"url": "https://cdn.max.test/p.gif"}}
                                ],
                            },
                        },
                    }
                )
            self.assertEqual(response.status_code, 200)
            kwargs = client.send_message.call_args.kwargs
            self.assertIn("Не удалось принять фото", kwargs["text"])
            self.assertIn("JPEG, PNG и WebP", kwargs["text"])
            self.assertNotIn("buttons", kwargs)
            self.assertEqual(Order.objects.get().photos.count(), 0)

    def test_photo_prompt_explains_clear_face_and_angles(self):
        from apps.max_bot.views import PHOTO_PROMPT

        self.assertIn("чётк", PHOTO_PROMPT)
        self.assertIn("лицо", PHOTO_PROMPT)
        self.assertIn("ракурс", PHOTO_PROMPT)
        self.assertIn("Фото загружены", PHOTO_PROMPT)

