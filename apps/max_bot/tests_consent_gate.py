"""MAX Pilot consent gate: photos_done → consent screen → consent:accept →
summary + checkout. Checkout fails closed without recorded consent; accept is
idempotent and bound to one order.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.core.services.channel_order_flow import (
    PILOT_CONSENT_VERSION,
    ChannelFlowError,
    ChannelOrderFlowService,
)
from apps.max_bot.payments import CheckoutSession, MaxExternalPaymentAdapter, MaxPaymentError

WEBHOOK_URL = "/max/webhook/"
USER_ID = 7101
CHAT_ID = 9101


def _callback(payload, callback_id="cb-1"):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": USER_ID, "first_name": "Ivan"},
        },
        "message": {"recipient": {"chat_id": CHAT_ID}, "body": {"mid": "mid-1"}},
    }


def _photo_message(url="https://cdn.max.test/p.jpg"):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": USER_ID, "first_name": "Ivan"},
            "recipient": {"chat_id": CHAT_ID},
            "body": {"mid": "mid-photo", "attachments": [{"type": "image", "payload": {"url": url}}]},
        },
    }


class FakeCheckoutProvider:
    name = "fake-external"

    def __init__(self):
        self.calls = 0

    def create_checkout(self, *, payment):
        self.calls += 1
        return CheckoutSession(checkout_url=f"https://pay.example.test/{payment.pk}", provider_reference="ref")

    def parse_webhook(self, *, body, signature):  # pragma: no cover - not used here
        raise NotImplementedError


class MaxConsentWebhookFlowTests(TestCase):
    def setUp(self):
        Product.objects.create(code="single-sticker", name="Один стикер", config={"price_minor": 10000, "currency": "RUB"})
        Style.objects.create(code="comic", name="Комикс")
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def _order_with_photo(self, client):
        self._post(_callback("product:single-sticker"))
        self._post(_callback("style:single-sticker:comic"))
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                with mock.patch("apps.max_bot.views.download_photo", return_value=b"image-bytes"):
                    self.assertEqual(self._post(_photo_message()).status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        client.reset_mock()
        return order

    def test_photos_done_shows_consent_and_does_not_start_checkout(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            order = self._order_with_photo(client)

            response = self._post(_callback("photos_done", callback_id="cb-done"))

            self.assertEqual(response.status_code, 200)
            kwargs = client.send_message.call_args.kwargs
            self.assertIn("право использовать загруженные фотографии", kwargs["text"])
            self.assertIn("обработаны для создания заказанных стикеров", kwargs["text"])
            self.assertIn("условия сервиса и заказа", kwargs["text"])
            self.assertEqual(kwargs["buttons"], [[{"text": "Принимаю", "payload": "consent:accept"}]])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            self.assertFalse(order.consent_accepted)
            checkout_mock.assert_not_called()
            client.answer_callback.assert_called_once_with(callback_id="cb-done")

    def test_photos_done_without_photos_is_rejected_before_consent(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            self._post(_callback("product:single-sticker"))
            self._post(_callback("style:single-sticker:comic"))
            client.reset_mock()

            response = self._post(_callback("photos_done"))

            self.assertEqual(response.status_code, 409)
            client.send_message.assert_not_called()
            checkout_mock.assert_not_called()

    def test_accept_persists_consent_and_starts_checkout(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))
            client.reset_mock()

            response = self._post(_callback("consent:accept", callback_id="cb-accept"))

            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
            self.assertEqual(order.consent_version, PILOT_CONSENT_VERSION)
            self.assertIsNotNone(order.consent_accepted_at)
            self.assertIn("100 ₽", client.send_message.call_args.kwargs["text"])
            checkout_mock.assert_called_once()
            self.assertEqual(checkout_mock.call_args.kwargs["chat_id"], str(CHAT_ID))
            client.answer_callback.assert_called_once_with(callback_id="cb-accept")

    def test_accept_without_photos_is_rejected_and_nothing_persisted(self):
        with mock.patch("apps.max_bot.views.MaxBotClient"), mock.patch(
            "apps.max_bot.views.start_checkout"
        ) as checkout_mock:
            self._post(_callback("product:single-sticker"))
            self._post(_callback("style:single-sticker:comic"))

            response = self._post(_callback("consent:accept"))

            self.assertEqual(response.status_code, 409)
            order = Order.objects.get()
            self.assertFalse(order.consent_accepted)
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            checkout_mock.assert_not_called()

    def test_duplicate_accept_is_idempotent(self):
        provider = FakeCheckoutProvider()
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.checkout.YooKassaPaymentProvider.from_env", return_value=provider
        ):
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))

            first = self._post(_callback("consent:accept", callback_id="cb-a1"))
            order.refresh_from_db()
            accepted_at = order.consent_accepted_at
            second = self._post(_callback("consent:accept", callback_id="cb-a2"))

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.consent_accepted_at, accepted_at)
            self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
            # one pending payment / one provider session, the link is reused
            self.assertEqual(Payment.objects.filter(order=order).count(), 1)
            self.assertEqual(provider.calls, 1)
            urls = [
                call.kwargs["buttons"][0][0]["url"]
                for call in client.send_message.call_args_list
                if call.kwargs.get("buttons") and call.kwargs["buttons"][0][0].get("url")
            ]
            self.assertEqual(len(urls), 2)
            self.assertEqual(urls[0], urls[1])


class ConsentDomainTests(TestCase):
    def setUp(self):
        self.flow = ChannelOrderFlowService()
        self.identity = self.flow.get_or_create_identity(
            channel=ChannelIdentity.Channel.MAX, external_user_id="max-consent-1"
        )
        self.product = Product.objects.create(
            code="single-sticker", name="Один стикер", config={"price_minor": 10000, "currency": "RUB"}
        )
        self.style = Style.objects.create(code="comic", name="Комикс")

    def _order_with_photo(self):
        order = self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")
        order.photos.create(storage_key=f"orders/{order.pk}/p.jpg", mime_type="image/jpeg", size_bytes=10)
        return order

    def test_checkout_fails_closed_without_consent(self):
        order = self._order_with_photo()
        self.flow.complete_photos(self.identity)
        adapter = MaxExternalPaymentAdapter(provider=FakeCheckoutProvider())

        with self.assertRaisesMessage(MaxPaymentError, "consent"):
            adapter.create_checkout(identity=self.identity)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
        self.assertFalse(Payment.objects.filter(order=order).exists())

    def test_accepted_order_can_proceed_to_checkout(self):
        order = self._order_with_photo()
        self.flow.accept_consent(identity=self.identity)
        self.flow.complete_photos(self.identity)
        adapter = MaxExternalPaymentAdapter(provider=FakeCheckoutProvider())

        payment, session = adapter.create_checkout(identity=self.identity)

        self.assertEqual(payment.order_id, order.pk)
        self.assertTrue(session.checkout_url.startswith("https://pay.example.test/"))

    def test_accept_requires_complete_photos(self):
        self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")

        with self.assertRaises(ChannelFlowError):
            self.flow.accept_consent(identity=self.identity)

        self.assertFalse(Order.objects.get().consent_accepted)

    def test_accept_is_idempotent_and_keeps_first_timestamp(self):
        self._order_with_photo()
        first = self.flow.accept_consent(identity=self.identity)
        second = self.flow.accept_consent(identity=self.identity)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.consent_accepted_at, second.consent_accepted_at)

    def test_another_order_does_not_inherit_consent(self):
        first = self._order_with_photo()
        self.flow.accept_consent(identity=self.identity)
        self.flow.complete_photos(self.identity)
        first.status = Order.Status.CANCELLED
        first.save(update_fields=["status"])

        second = self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")

        self.assertNotEqual(first.pk, second.pk)
        self.assertFalse(second.consent_accepted)
        self.assertEqual(second.consent_version, "")
        self.assertIsNone(second.consent_accepted_at)

    def test_consent_accepted_requires_both_fields(self):
        order = self._order_with_photo()
        order.consent_accepted_at = timezone.now()
        self.assertFalse(order.consent_accepted)
        order.consent_version = PILOT_CONSENT_VERSION
        self.assertTrue(order.consent_accepted)
