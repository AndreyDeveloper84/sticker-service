"""Telegram consent gate (parity with MAX, DRF-2069): photos_done → consent
screen → consent:accept → summary → pay (Stars invoice). The invoice fails
closed without recorded consent; accept is idempotent and bound to one order.

The PAID confirmation after successful_payment already existed in the
Telegram flow («Оплата получена. Начинаем подготовку превью.») and is only
asserted here, not duplicated.
"""

import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style
from apps.core.services.channel_order_flow import (
    PILOT_CONSENT_TEXT,
    PILOT_CONSENT_VERSION,
    ChannelFlowError,
    ChannelOrderFlowService,
)
from apps.telegram_bot.paid_notice import PAID_NOTICE_TEXT
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter

WEBHOOK_URL = "/telegram/webhook/"
USER = {"id": 3201, "first_name": "Olga"}
CHAT_ID = 4201
SINGLE_CONFIG = {
    "kind": "single",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": [{"code": "hello", "label": "Привет"}],
    "price_minor": 10000,
    "price_stars": 100,
    "currency": "RUB",
}


def _callback(data, callback_id="cb-1"):
    return {"callback_query": {"id": callback_id, "from": USER, "message": {"chat": {"id": CHAT_ID}}, "data": data}}


def _photo():
    return {"message": {"from": USER, "chat": {"id": CHAT_ID}, "photo": [{"file_id": "big"}]}}


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramConsentWebhookFlowTests(TestCase):
    def setUp(self):
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")

    def _post(self, payload):
        return self.client.post(WEBHOOK_URL, data=json.dumps(payload), content_type="application/json")

    def _order_with_photo(self, client):
        client.get_file.return_value = {"file_path": "photos/source.jpg"}
        client.download_file.return_value = b"image-bytes"
        self._post(_callback("product:single-sticker"))
        self._post(_callback("style:single-sticker:comic"))
        self._post(_callback("emotion:hello"))
        self.assertEqual(self._post(_photo()).status_code, 200)
        order = Order.objects.get()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        client.reset_mock()
        return order

    def test_photos_done_shows_consent_and_sends_no_invoice(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._order_with_photo(client)

            response = self._post(_callback("photos_done", "cb-done"))

            self.assertEqual(response.status_code, 200)
            kwargs = client.send_message.call_args.kwargs
            self.assertEqual(kwargs["text"], PILOT_CONSENT_TEXT)
            self.assertEqual(
                kwargs["reply_markup"], {"inline_keyboard": [[{"text": "Принимаю", "callback_data": "consent:accept"}]]}
            )
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
            self.assertFalse(order.consent_accepted)
            client.send_invoice.assert_not_called()
            client.answer_callback_query.assert_called_once_with(callback_query_id="cb-done")

    def test_pay_without_consent_sends_no_invoice(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))
            # force the order past the consent screen without consent
            ChannelOrderFlowService().complete_photos(order.channel_identity)
            client.reset_mock()

            response = self._post(_callback("pay"))

            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["ok"])
            self.assertIn("consent", response.json()["error"])
            client.send_invoice.assert_not_called()
            self.assertFalse(Payment.objects.filter(order=order).exists())

    def test_accept_persists_consent_then_pay_sends_invoice(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))
            client.reset_mock()

            response = self._post(_callback("consent:accept", "cb-accept"))

            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
            self.assertEqual(order.consent_version, PILOT_CONSENT_VERSION)
            self.assertIsNotNone(order.consent_accepted_at)
            kwargs = client.send_message.call_args.kwargs
            self.assertIn("100 Stars", kwargs["text"])
            self.assertEqual(kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "pay")
            client.send_invoice.assert_not_called()

            client.reset_mock()
            self.assertEqual(self._post(_callback("pay")).status_code, 200)
            self.assertEqual(client.send_invoice.call_args.kwargs["amount_stars"], 100)
            payment = Payment.objects.get(order=order)
            self.assertEqual((payment.amount_minor, payment.currency), (100, "XTR"))

    def test_accept_without_photos_is_rejected_and_nothing_persisted(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient"):
            self._post(_callback("product:single-sticker"))
            self._post(_callback("style:single-sticker:comic"))
            self._post(_callback("emotion:hello"))

            response = self._post(_callback("consent:accept"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ok"], True)
            order = Order.objects.get()
            self.assertFalse(order.consent_accepted)
            self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

    def test_duplicate_accept_is_idempotent_and_pay_reuses_payment(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))

            first = self._post(_callback("consent:accept", "cb-a1"))
            order.refresh_from_db()
            accepted_at = order.consent_accepted_at
            self.assertEqual(self._post(_callback("pay")).status_code, 200)
            second = self._post(_callback("consent:accept", "cb-a2"))
            self.assertEqual(self._post(_callback("pay")).status_code, 200)

            self.assertEqual((first.status_code, second.status_code), (200, 200))
            order.refresh_from_db()
            self.assertEqual(order.consent_accepted_at, accepted_at)
            self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
            self.assertEqual(Payment.objects.filter(order=order).count(), 1)
            payloads = {call.kwargs["payload"] for call in client.send_invoice.call_args_list}
            self.assertEqual(len(client.send_invoice.call_args_list), 2)
            self.assertEqual(len(payloads), 1)

    def test_successful_payment_confirmation_message_already_exists(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            order = self._order_with_photo(client)
            self._post(_callback("photos_done"))
            self._post(_callback("consent:accept"))
            self._post(_callback("pay"))
            payment = Payment.objects.get(order=order)
            client.reset_mock()

            update = {
                "message": {
                    "from": USER,
                    "chat": {"id": CHAT_ID},
                    "successful_payment": {
                        "currency": "XTR",
                        "total_amount": 100,
                        "invoice_payload": TelegramStarsPaymentAdapter.payload(payment),
                        "telegram_payment_charge_id": "charge-1",
                    },
                }
            }
            self.assertEqual(self._post(update).status_code, 200)

            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PAID)
            self.assertEqual(client.send_message.call_args.kwargs["text"], PAID_NOTICE_TEXT)


class TelegramConsentDomainTests(TestCase):
    def setUp(self):
        self.flow = ChannelOrderFlowService()
        self.identity = self.flow.get_or_create_identity(
            channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="tg-consent-1"
        )
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")
        self.payments = TelegramStarsPaymentAdapter()

    def _order_with_photo(self):
        order = self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")
        self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        order.photos.create(storage_key=f"orders/{order.pk}/p.jpg", mime_type="image/jpeg", size_bytes=10)
        return order

    def test_stars_checkout_fails_closed_without_consent(self):
        order = self._order_with_photo()
        self.flow.complete_photos(self.identity)

        with self.assertRaisesMessage(TelegramPaymentError, "consent"):
            self.payments.payment_for_identity(self.identity)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
        self.assertFalse(Payment.objects.filter(order=order).exists())

    def test_accepted_order_gets_a_pending_stars_payment(self):
        order = self._order_with_photo()
        self.flow.accept_consent(identity=self.identity)
        self.flow.complete_photos(self.identity)

        payment = self.payments.payment_for_identity(self.identity)

        self.assertEqual(payment.order_id, order.pk)
        self.assertEqual((payment.amount_minor, payment.currency), (100, "XTR"))

    def test_accept_requires_complete_photos_and_is_idempotent(self):
        self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")
        with self.assertRaises(ChannelFlowError):
            self.flow.accept_consent(identity=self.identity)
        self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        order = Order.objects.get()
        order.photos.create(storage_key=f"orders/{order.pk}/p.jpg", mime_type="image/jpeg", size_bytes=10)

        first = self.flow.accept_consent(identity=self.identity)
        second = self.flow.accept_consent(identity=self.identity)

        self.assertEqual(first.consent_accepted_at, second.consent_accepted_at)

    def test_next_order_does_not_inherit_consent(self):
        first = self._order_with_photo()
        self.flow.accept_consent(identity=self.identity)
        self.flow.complete_photos(self.identity)
        first.status = Order.Status.CANCELLED
        first.save(update_fields=["status"])

        second = self.flow.create_or_get_order(identity=self.identity, product_code="single-sticker", style_code="comic")

        self.assertNotEqual(first.pk, second.pk)
        self.assertFalse(second.consent_accepted)
