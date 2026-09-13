from django.test import TestCase

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.core.services import (
    InvalidOrderTransition,
    OrderStateService,
    PaymentError,
    PaymentService,
)


class PaymentServiceTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-payment-1",
        )
        product = Product.objects.create(code="stickers", name="Sticker Pack")
        style = Style.objects.create(code="classic", name="Classic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )

    def test_create_pending_snapshots_amount_and_moves_order(self):
        payment = PaymentService.create_pending(
            order=self.order,
            provider="telegram_stars",
            amount_minor=499,
            currency="xtr",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payment.amount_minor, 499)
        self.assertEqual(payment.currency, "XTR")

    def test_direct_paid_transition_is_rejected(self):
        self.order.status = Order.Status.AWAITING_PAYMENT
        self.order.save(update_fields=["status"])

        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=self.order,
                to_status=Order.Status.PAID,
            )

    def test_confirm_marks_payment_and_order_paid(self):
        payment = PaymentService.create_pending(
            order=self.order,
            provider="telegram_stars",
            amount_minor=499,
            currency="XTR",
        )

        confirmed = PaymentService.confirm(
            payment=payment,
            external_payment_id="tg-charge-1",
            metadata={"source": "successful_payment"},
        )

        self.order.refresh_from_db()
        self.assertEqual(confirmed.status, Payment.Status.CONFIRMED)
        self.assertEqual(confirmed.external_payment_id, "tg-charge-1")
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_duplicate_confirmation_is_idempotent(self):
        payment = PaymentService.create_pending(
            order=self.order,
            provider="telegram_stars",
            amount_minor=499,
            currency="XTR",
        )

        first = PaymentService.confirm(
            payment=payment,
            external_payment_id="tg-charge-2",
        )
        second = PaymentService.confirm(
            payment=payment,
            external_payment_id="tg-charge-2",
        )

        self.order.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CONFIRMED).count(), 1)
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_provider_transaction_cannot_bind_to_two_payments(self):
        first = PaymentService.create_pending(
            order=self.order,
            provider="external",
            amount_minor=10000,
            currency="RUB",
        )
        PaymentService.confirm(
            payment=first,
            external_payment_id="provider-transaction-1",
        )

        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-payment-2",
        )
        second_order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=self.order.product,
            style=self.order.style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )
        second = PaymentService.create_pending(
            order=second_order,
            provider="external",
            amount_minor=10000,
            currency="RUB",
        )

        with self.assertRaises(PaymentError):
            PaymentService.confirm(
                payment=second,
                external_payment_id="provider-transaction-1",
            )

        second_order.refresh_from_db()
        self.assertEqual(second_order.status, Order.Status.AWAITING_PAYMENT)

    def test_payment_snapshot_is_not_derived_from_later_product_changes(self):
        payment = PaymentService.create_pending(
            order=self.order,
            provider="external",
            amount_minor=12500,
            currency="RUB",
        )

        product = self.order.product
        product.config = {"price_minor": 99999, "currency": "RUB"}
        product.save(update_fields=["config"])

        payment.refresh_from_db()
        self.assertEqual(payment.amount_minor, 12500)
        self.assertEqual(payment.currency, "RUB")

    def test_terminal_m1_states_remain_terminal(self):
        for terminal_status in (Order.Status.CANCELLED, Order.Status.FAILED):
            self.order.status = terminal_status
            self.order.save(update_fields=["status"])
            with self.assertRaises(InvalidOrderTransition):
                OrderStateService.transition(
                    order=self.order,
                    to_status=Order.Status.AWAITING_PAYMENT,
                )
