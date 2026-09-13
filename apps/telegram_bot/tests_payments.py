from unittest.mock import Mock

from django.test import TestCase

from apps.core.models import Order, Payment, Product, Style
from apps.telegram_bot.adapter import TelegramAdapter
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter


class TelegramStarsPaymentTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(code="stickers", name="Sticker Pack", config={"price_stars": 150})
        self.style = Style.objects.create(code="classic", name="Classic")
        self.telegram = TelegramAdapter()
        self.payments = TelegramStarsPaymentAdapter()
        self.identity = self.telegram.get_or_create_identity({"id": 1001, "username": "buyer", "first_name": "Buyer"})
        self.order = Order.objects.create(user=self.identity.user, channel_identity=self.identity, product=self.product, style=self.style, status=Order.Status.READY_FOR_CHECKOUT)

    def test_pending_payment_uses_server_side_stars_price_and_is_reused(self):
        first = self.payments.payment_for_identity(self.identity)
        second = self.payments.payment_for_identity(self.identity)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.currency, "XTR")
        self.assertEqual(first.amount_minor, 150)
        self.assertEqual(Payment.objects.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_pre_checkout_validates_payload_currency_and_amount(self):
        payment = self.payments.payment_for_identity(self.identity)
        query = {"invoice_payload": self.payments.payload(payment), "currency": "XTR", "total_amount": 150}
        validated = self.payments.validate_pre_checkout(identity=self.identity, query=query)
        self.assertEqual(validated.pk, payment.pk)
        query["total_amount"] = 151
        with self.assertRaises(TelegramPaymentError):
            self.payments.validate_pre_checkout(identity=self.identity, query=query)

    def test_payment_payload_cannot_be_used_by_another_telegram_user(self):
        payment = self.payments.payment_for_identity(self.identity)
        other = self.telegram.get_or_create_identity({"id": 2002, "username": "other", "first_name": "Other"})
        with self.assertRaises(TelegramPaymentError):
            self.payments.validate_pre_checkout(identity=other, query={"invoice_payload": self.payments.payload(payment), "currency": "XTR", "total_amount": 150})

    def test_successful_payment_confirms_payment_and_order_idempotently(self):
        payment = self.payments.payment_for_identity(self.identity)
        successful = {"invoice_payload": self.payments.payload(payment), "currency": "XTR", "total_amount": 150, "telegram_payment_charge_id": "tg-charge-1", "provider_payment_charge_id": ""}
        first = self.payments.confirm_successful_payment(identity=self.identity, successful_payment=successful)
        second = self.payments.confirm_successful_payment(identity=self.identity, successful_payment=successful)
        self.assertEqual(first.pk, second.pk)
        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(payment.external_payment_id, "tg-charge-1")
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_successful_payment_rejects_tampered_amount(self):
        payment = self.payments.payment_for_identity(self.identity)
        with self.assertRaises(TelegramPaymentError):
            self.payments.confirm_successful_payment(identity=self.identity, successful_payment={"invoice_payload": self.payments.payload(payment), "currency": "XTR", "total_amount": 999, "telegram_payment_charge_id": "tg-charge-bad"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_send_invoice_uses_xtr_without_provider_token(self):
        client = TelegramBotClient("test-token")
        client._post = Mock(return_value={"message_id": 1})
        client.send_invoice(chat_id=1001, title="Sticker Pack", description="Digital order", payload="order:1:payment:1", amount_stars=150)
        method, payload = client._post.call_args.args
        self.assertEqual(method, "sendInvoice")
        self.assertEqual(payload["currency"], "XTR")
        self.assertNotIn("provider_token", payload)
        self.assertEqual(payload["prices"], [{"label": "Sticker Pack", "amount": 150}])
