"""«Возврат Telegram Stars из консоли» (owner GO 2026-09-20).

Client: refundStarPayment through the usual transport. Service:
PaymentService.refund — CONFIRMED telegram_stars only, at-most-once, a
provider refusal changes nothing. Console: «Вернуть звёзды» for a confirmed
Stars payment only, superuser only, with a mandatory reason. Economics: a
refunded payment is a proven 0 — «возвращено: 100 XTR», never "unknown".
"""

import csv
import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Order, OrderEvent, Payment, Product, Style
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION
from apps.core.services.order_economics import NOT_COMPUTABLE, OrderEconomics
from apps.core.services.payment import PaymentError, PaymentService
from apps.core.services.pilot_analytics import PilotAnalyticsService, period_for
from apps.core.services.pilot_metrics import PilotMetricsService
from apps.telegram_bot.adapter import TelegramAdapter
from apps.telegram_bot.client import DEFAULT_API_ORIGIN, TelegramAPIError, TelegramBotClient
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter
from apps.telegram_bot.tests_client import FakeResponse, _fake_http

TOKEN = "123:refund-token"


class RefundStarPaymentClientTests(SimpleTestCase):
    def test_refund_star_payment_calls_the_bot_api(self):
        patcher, fake = _fake_http(response=FakeResponse(payload={"ok": True, "result": True}))
        with patcher:
            result = TelegramBotClient(TOKEN).refund_star_payment(user_id="1001", telegram_payment_charge_id="tg-charge-1")
        self.assertIs(result, True)
        args, kwargs = fake.post.call_args
        self.assertEqual(args[0], f"{DEFAULT_API_ORIGIN}/bot{TOKEN}/refundStarPayment")
        self.assertEqual(kwargs["json"], {"user_id": "1001", "telegram_payment_charge_id": "tg-charge-1"})

    def test_provider_refusal_raises_with_telegram_description(self):
        patcher, _fake = _fake_http(response=FakeResponse(
            status_code=200, payload={"ok": False, "description": "Bad Request: CHARGE_ALREADY_REFUNDED"},
        ))
        with patcher, self.assertRaises(TelegramAPIError) as ctx:
            TelegramBotClient(TOKEN).refund_star_payment(user_id="1001", telegram_payment_charge_id="tg-charge-1")
        self.assertEqual(ctx.exception.description, "Bad Request: CHARGE_ALREADY_REFUNDED")


class StarsFixture(TestCase):
    def setUp(self):
        self.product = Product.objects.create(code="single-sticker", name="1 стикер", config={"price_stars": 100, "price_minor": 10000})
        self.style = Style.objects.create(code="meme", name="Мемные")
        self.telegram = TelegramAdapter()
        self.stars = TelegramStarsPaymentAdapter()
        self.identity = self.telegram.get_or_create_identity({"id": 1001, "username": "buyer", "first_name": "Buyer"})
        self.order = Order.objects.create(
            user=self.identity.user, channel_identity=self.identity, product=self.product, style=self.style,
            status=Order.Status.READY_FOR_CHECKOUT, consent_version=PILOT_CONSENT_VERSION, consent_accepted_at=timezone.now(),
        )
        payment = self.stars.payment_for_identity(self.identity)
        self.payment = self.stars.confirm_successful_payment(identity=self.identity, successful_payment={
            "invoice_payload": self.stars.payload(payment), "currency": "XTR", "total_amount": 100,
            "telegram_payment_charge_id": "tg-charge-17", "provider_payment_charge_id": "",
        })
        self.client_mock = mock.Mock(spec=TelegramBotClient)
        self.client_mock.refund_star_payment.return_value = True

    def refund(self, **kwargs):
        return self.stars.refund(payment=self.payment, client=self.client_mock, actor_ref="op", reason="тест владельца", **kwargs)


class RefundServiceTests(StarsFixture):
    def test_refund_marks_payment_refunded_records_metadata_and_event_once(self):
        refunded = self.refund()
        self.client_mock.refund_star_payment.assert_called_once_with(user_id="1001", telegram_payment_charge_id="tg-charge-17")
        self.assertEqual(refunded.status, Payment.Status.REFUNDED)
        details = refunded.metadata["refund"]
        self.assertEqual(details["charge_id"], "tg-charge-17")
        self.assertEqual(details["actor_ref"], "op")
        self.assertEqual(details["reason"], "тест владельца")
        self.assertTrue(details["refunded_at"])
        self.assertEqual(details["provider_response"], {"ok": True, "result": True})
        self.assertEqual(refunded.amount_minor, 100)  # amounts are never rewritten
        self.assertEqual(refunded.external_payment_id, "tg-charge-17")
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.PAYMENT_REFUNDED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "op")
        self.assertEqual(event.payload, {"payment_id": refunded.pk, "amount_minor": 100, "currency": "XTR", "reason": "тест владельца"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID, "the order status is the operator's separate decision")

        # at-most-once: the second call is refused BEFORE the provider
        with self.assertRaisesMessage(TelegramPaymentError, "already refunded"):
            self.refund()
        self.assertEqual(self.client_mock.refund_star_payment.call_count, 1)
        self.assertEqual(OrderEvent.objects.filter(event_type=OrderEvent.PAYMENT_REFUNDED).count(), 1)

    def test_provider_error_changes_nothing(self):
        self.client_mock.refund_star_payment.side_effect = TelegramAPIError(
            "refundStarPayment", status_code=400, description="Bad Request: CHARGE_NOT_FOUND",
        )
        with self.assertRaisesMessage(TelegramPaymentError, "CHARGE_NOT_FOUND"):
            self.refund()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.assertNotIn("refund", self.payment.metadata)
        self.assertFalse(OrderEvent.objects.filter(event_type=OrderEvent.PAYMENT_REFUNDED).exists())

    def test_only_confirmed_telegram_stars_payments_are_refundable(self):
        yookassa = Payment.objects.create(
            order=self.order, provider="yookassa", status=Payment.Status.CONFIRMED, amount_minor=10000, currency="RUB",
            external_payment_id="yk-1", confirmed_at=timezone.now(),
        )
        with self.assertRaisesMessage(PaymentError, "not supported for provider yookassa"):
            PaymentService.refund(payment=yookassa, actor_ref="op", reason="x", provider_refund=lambda p: True)
        pending = Payment.objects.create(order=self.order, provider="telegram_stars", status=Payment.Status.PENDING, amount_minor=100, currency="XTR")
        with self.assertRaisesMessage(PaymentError, "cannot be refunded from pending"):
            PaymentService.refund(payment=pending, actor_ref="op", reason="x", provider_refund=lambda p: True)
        with self.assertRaisesMessage(PaymentError, "reason is required"):
            PaymentService.refund(payment=self.payment, actor_ref="op", reason="  ", provider_refund=lambda p: True)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)

    def test_refund_without_a_charge_id_is_refused_before_the_provider(self):
        Payment.objects.filter(pk=self.payment.pk).update(external_payment_id="", metadata={})
        self.payment.refresh_from_db()
        with self.assertRaisesMessage(TelegramPaymentError, "no telegram_payment_charge_id"):
            self.refund()
        self.client_mock.refund_star_payment.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)


@override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
class RefundConsoleTests(StarsFixture):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.superuser = User.objects.create_superuser(username="root", email="root@example.com", password="pass")
        self.staff = User.objects.create_user(username="op", email="op@example.com", password="pass", is_staff=True)
        for perm in ("view_order", "change_order"):
            from django.contrib.auth.models import Permission
            self.staff.user_permissions.add(Permission.objects.get(codename=perm))
        self.change_url = reverse("admin:core_order_change", args=[self.order.pk])
        self.refund_url = reverse("admin:core_order_refund_stars", args=[self.order.pk])

    def test_button_is_shown_for_a_confirmed_stars_payment_only(self):
        self.client.force_login(self.superuser)
        content = self.client.get(self.change_url).content.decode()
        self.assertIn("Оплачено: 100 XTR (telegram_stars)", content)
        self.assertIn("Вернуть звёзды", content)
        self.assertIn(self.refund_url, content)

        Payment.objects.filter(pk=self.payment.pk).update(provider="yookassa", currency="RUB", amount_minor=10000)
        content = self.client.get(self.change_url).content.decode()
        self.assertIn("Оплачено: 100,00 ₽ (yookassa)", content)
        self.assertNotIn("Вернуть звёзды", content)

    def test_confirmation_page_shows_the_amount_and_requires_a_reason(self):
        self.client.force_login(self.superuser)
        with mock.patch("apps.core.production_console.TelegramBotClient", return_value=self.client_mock):
            page = self.client.get(self.refund_url)
            self.assertEqual(page.status_code, 200)
            content = page.content.decode()
            self.assertIn("Вернуть звёзды", content)
            self.assertIn("будут возвращены 100 XTR", content)
            self.assertIn('name="reason"', content)
            self.assertIn('value="Вернуть 100 XTR"', content)

            response = self.client.post(self.refund_url, {"reason": "   "})
            self.assertEqual(response.status_code, 200, "no reason → the form again")
            self.assertIn("Укажите причину возврата.", response.content.decode())
            self.client_mock.refund_star_payment.assert_not_called()

            response = self.client.post(self.refund_url, {"reason": "тест владельца, Order 17"}, follow=True)
        self.assertRedirects(response, self.change_url)
        content = response.content.decode()
        self.assertIn("Возвращено 100 XTR клиенту", content)
        self.assertIn("Оплата возвращена", content)
        self.assertIn("причина: тест владельца, Order 17", content)
        self.assertNotIn("Вернуть звёзды</a>", content)
        self.client_mock.refund_star_payment.assert_called_once_with(user_id="1001", telegram_payment_charge_id="tg-charge-17")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(self.payment.metadata["refund"]["actor_ref"], "root")
        self.assertIn("Выручка: 0 XTR — возврат (возвращено: 100 XTR, telegram_stars)", content)

    def test_provider_refusal_is_reported_and_nothing_changes(self):
        self.client.force_login(self.superuser)
        self.client_mock.refund_star_payment.side_effect = TelegramAPIError(
            "refundStarPayment", status_code=400, description="Bad Request: CHARGE_ALREADY_REFUNDED",
        )
        with mock.patch("apps.core.production_console.TelegramBotClient", return_value=self.client_mock):
            response = self.client.post(self.refund_url, {"reason": "x"}, follow=True)
        self.assertIn("Возврат не выполнен: Telegram refused the refund: Bad Request: CHARGE_ALREADY_REFUNDED", response.content.decode())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)

    def test_staff_without_superuser_cannot_refund(self):
        self.client.force_login(self.staff)
        with mock.patch("apps.core.production_console.TelegramBotClient", return_value=self.client_mock):
            response = self.client.post(self.refund_url, {"reason": "x"}, follow=True)
        self.assertIn("только суперпользователь", response.content.decode())
        self.client_mock.refund_star_payment.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)

    def test_refund_link_without_a_refundable_payment_says_so(self):
        self.client.force_login(self.superuser)
        self.refund()
        response = self.client.get(self.refund_url, follow=True)
        self.assertIn("Нет подтверждённого платежа Telegram Stars для возврата (или он уже возвращён).", response.content.decode())


class RefundEconomicsTests(StarsFixture):
    def test_refunded_payment_is_a_proven_zero_with_the_returned_amount_named(self):
        before = OrderEconomics.compute(self.order)
        self.assertEqual(before["revenue"], {"amount_minor": 100, "currency": "XTR"})
        self.assertIsNone(before["refund"])

        self.refund()
        eco = OrderEconomics.compute(Order.objects.get(pk=self.order.pk))
        self.assertEqual(eco["revenue"], {"amount_minor": 0, "currency": "XTR"})
        self.assertEqual(eco["refund"]["amount_minor"], 100)
        self.assertEqual(eco["refund"]["currency"], "XTR")
        self.assertEqual(eco["refund"]["reason"], "тест владельца")
        self.assertEqual(eco["payment"]["status"], Payment.Status.REFUNDED)
        self.assertNotIn("no_confirmed_payment", eco["unknown_components"])
        self.assertEqual(eco["known_contribution_minor"], NOT_COMPUTABLE)  # XTR, as before

    def test_pilot_analytics_keeps_the_order_paid_and_lists_the_refund_apart(self):
        self.refund()
        service = PilotAnalyticsService(period_for("today"))
        snapshot = service.snapshot()
        self.assertEqual(snapshot["funnel"]["steps"][1]["step"], "payment")
        self.assertEqual(snapshot["funnel"]["steps"][1]["count"], 1, "the order did reach payment")
        self.assertEqual(snapshot["revenue"]["cells"], [])
        self.assertEqual(snapshot["revenue"]["totals"], [])
        self.assertEqual(snapshot["revenue"]["refunded"], [{"currency": "XTR", "orders": 1, "amount_minor": 100}])
        rows = service.export_rows()
        self.assertEqual(rows[0]["revenue"], 0)
        self.assertEqual(rows[0]["currency"], "XTR")
        metrics = PilotMetricsService().snapshot()
        self.assertEqual(metrics["payments"]["refunded"], 1)

    def test_pilot_metrics_page_shows_the_refunded_line(self):
        self.refund()
        User = get_user_model()
        self.client.force_login(User.objects.create_superuser(username="root", email="r@example.com", password="pass"))
        content = self.client.get(reverse("admin:core_order_pilot_metrics") + "?preset=today").content.decode()
        self.assertIn("Возвращено (в выручку не входит)", content)
        self.assertIn("100 XTR (1)", content)
