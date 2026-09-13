from django.test import TestCase

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.max_bot.client_payment_link import render_button
from apps.max_bot.payments import (
    CheckoutSession,
    MaxExternalPaymentAdapter,
    MaxPaymentError,
    PaymentConfirmation,
)


class FakeProvider:
    name = "external-test"

    def __init__(self):
        self.create_count = 0
        self.confirmation = None

    def create_checkout(self, *, payment):
        self.create_count += 1
        return CheckoutSession(
            checkout_url=f"https://pay.example.test/{payment.pk}",
            provider_reference=f"session-{payment.pk}",
        )

    def parse_webhook(self, *, body, signature):
        return self.confirmation


class MaxExternalPaymentTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-100",
        )
        product = Product.objects.create(
            code="stickers",
            name="Sticker Pack",
            config={"price_minor": 19900, "currency": "RUB"},
        )
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=self.identity,
            product=product,
            style=style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )
        self.provider = FakeProvider()
        self.adapter = MaxExternalPaymentAdapter(provider=self.provider)

    def test_checkout_uses_server_side_price_snapshot(self):
        payment, session = self.adapter.create_checkout(identity=self.identity)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.amount_minor, 19900)
        self.assertEqual(payment.currency, "RUB")
        self.assertEqual(payment.provider, self.provider.name)
        self.assertEqual(session.checkout_url, f"https://pay.example.test/{payment.pk}")

    def test_repeated_checkout_reuses_pending_payment_and_url(self):
        first_payment, first_session = self.adapter.create_checkout(identity=self.identity)
        second_payment, second_session = self.adapter.create_checkout(identity=self.identity)

        self.assertEqual(first_payment.pk, second_payment.pk)
        self.assertEqual(first_session.checkout_url, second_session.checkout_url)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.provider.create_count, 1)

    def test_successful_provider_confirmation_marks_payment_and_order_paid(self):
        payment, _session = self.adapter.create_checkout(identity=self.identity)
        self.provider.confirmation = PaymentConfirmation(
            payment_id=payment.pk,
            external_payment_id="provider-tx-1",
            status="succeeded",
            metadata={"event": "paid"},
        )

        confirmed = self.adapter.confirm_webhook(body=b"{}", signature="sig")

        confirmed.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(confirmed.status, Payment.Status.CONFIRMED)
        self.assertEqual(confirmed.external_payment_id, "provider-tx-1")
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_duplicate_provider_webhook_is_idempotent(self):
        payment, _session = self.adapter.create_checkout(identity=self.identity)
        self.provider.confirmation = PaymentConfirmation(
            payment_id=payment.pk,
            external_payment_id="provider-tx-2",
            status="succeeded",
            metadata={},
        )

        first = self.adapter.confirm_webhook(body=b"{}", signature="sig")
        second = self.adapter.confirm_webhook(body=b"{}", signature="sig")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_non_success_provider_status_does_not_mark_order_paid(self):
        payment, _session = self.adapter.create_checkout(identity=self.identity)
        self.provider.confirmation = PaymentConfirmation(
            payment_id=payment.pk,
            external_payment_id="provider-tx-3",
            status="failed",
            metadata={},
        )

        with self.assertRaises(MaxPaymentError):
            self.adapter.confirm_webhook(body=b"{}", signature="sig")

        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_max_link_button_is_rendered_as_link(self):
        button = render_button({"text": "Оплатить", "url": "https://pay.example.test/1"})
        self.assertEqual(button["type"], "link")
        self.assertEqual(button["url"], "https://pay.example.test/1")
