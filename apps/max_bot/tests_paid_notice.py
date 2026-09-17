"""PAID confirmation to the MAX customer after the YooKassa webhook.

Best-effort and at most once: the first successful PAID transition sends
exactly one message; duplicate webhooks, mismatches, non-success statuses and
MAX send failures never produce a second message nor undo the payment.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION
from apps.max_bot.client import MaxAPIError
from apps.max_bot.paid_notice import PAID_NOTICE_KEY, PAID_NOTICE_TEXT, notify_customer_paid
from apps.max_bot.payments import MaxExternalPaymentAdapter
from apps.max_bot.tests_yookassa import FakeHttpClient, make_provider, webhook_body, yk_payment_object

WEBHOOK_URL = "/max/payment/webhook/"


def _max_envelope(mid, *, seq=None, text=""):
    """Real MAX Bot API ``POST /messages`` envelope (reference deployment)."""
    body = {"mid": mid, "seq": seq if seq is not None else 1, "text": text}
    return {"message": {"sender": {"user_id": 1}, "recipient": {"chat_id": 2}, "timestamp": 1, "body": body}}


class FakeMaxClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent = []

    def send_message(self, *, chat_id=None, user_id=None, text, buttons=None, attachments=None):
        if self.fail:
            raise MaxAPIError(502, "max down")
        self.sent.append({"chat_id": chat_id, "user_id": user_id, "text": text})
        return _max_envelope(f"mid-{len(self.sent)}", seq=len(self.sent))


class PaidNoticeWebhookTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-paid-1"
        )
        product = Product.objects.create(
            code="single-sticker", name="Один стикер", config={"price_minor": 19900, "currency": "RUB"}
        )
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=self.identity,
            product=product,
            style=style,
            status=Order.Status.READY_FOR_CHECKOUT,
            consent_version=PILOT_CONSENT_VERSION,
            consent_accepted_at=timezone.now(),
        )
        self.http = FakeHttpClient(
            created={"id": "yk-tx-1", "confirmation": {"confirmation_url": "https://pay.yookassa.test/abc"}}
        )
        self.provider = make_provider(self.http)
        self.payment, _ = MaxExternalPaymentAdapter(provider=self.provider).create_checkout(identity=self.identity)
        self.max_client = FakeMaxClient()

    def post_webhook(self, body=None, *, max_client=None):
        with patch(
            "apps.max_bot.provider_webhook.YooKassaPaymentProvider.from_env", return_value=self.provider
        ), patch("apps.max_bot.provider_webhook.MaxBotClient", return_value=max_client or self.max_client):
            return self.client.post(WEBHOOK_URL, data=body or webhook_body(), content_type="application/json")

    def _notice(self):
        self.payment.refresh_from_db()
        return (self.payment.metadata or {}).get(PAID_NOTICE_KEY) or {}

    def test_first_paid_sends_exactly_one_confirmation(self):
        self.http.payment_object = yk_payment_object(self.payment.pk)

        response = self.post_webhook()

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(len(self.max_client.sent), 1)
        self.assertEqual(self.max_client.sent[0]["user_id"], "max-paid-1")
        self.assertIsNone(self.max_client.sent[0]["chat_id"])
        self.assertEqual(self.max_client.sent[0]["text"], PAID_NOTICE_TEXT)
        notice = self._notice()
        self.assertEqual(notice["status"], "sent")
        self.assertEqual(notice["message_id"], "mid-1")

    def test_duplicate_webhook_does_not_send_twice(self):
        self.http.payment_object = yk_payment_object(self.payment.pk)

        first = self.post_webhook()
        second = self.post_webhook()

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CONFIRMED).count(), 1)
        self.assertEqual(len(self.max_client.sent), 1)

    def test_max_send_failure_keeps_payment_confirmed_and_acks_webhook(self):
        self.http.payment_object = yk_payment_object(self.payment.pk)
        failing = FakeMaxClient(fail=True)

        with self.assertLogs("apps.max_bot.paid_notice", level="WARNING"):
            response = self.post_webhook(max_client=failing)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        notice = self._notice()
        self.assertEqual(notice["status"], "failed")
        self.assertEqual(notice["error"], "MaxAPIError")

        # a later duplicate webhook does not retry: at most one customer message
        replay = self.post_webhook()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(len(self.max_client.sent), 0)

    def test_amount_mismatch_sends_nothing(self):
        self.http.payment_object = yk_payment_object(self.payment.pk, value="9.00")

        response = self.post_webhook()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.max_client.sent, [])
        self.assertEqual(self._notice(), {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_currency_mismatch_sends_nothing(self):
        self.http.payment_object = yk_payment_object(self.payment.pk, currency="USD")

        response = self.post_webhook()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.max_client.sent, [])

    def test_canceled_status_sends_nothing(self):
        self.http.payment_object = yk_payment_object(self.payment.pk, status="canceled")

        response = self.post_webhook()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": "status canceled"})
        self.assertEqual(self.max_client.sent, [])
        self.assertEqual(self._notice(), {})


class PaidNoticeUnitTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-paid-unit"
        )
        product = Product.objects.create(code="p", name="P", config={"price_minor": 10000, "currency": "RUB"})
        style = Style.objects.create(code="s", name="S")
        self.order = Order.objects.create(
            user=user, channel_identity=self.identity, product=product, style=style, status=Order.Status.PAID
        )

    def _payment(self, status=Payment.Status.CONFIRMED):
        return Payment.objects.create(
            order=self.order, provider="yookassa", amount_minor=10000, currency="RUB", status=status,
            external_payment_id="yk-1" if status == Payment.Status.CONFIRMED else "",
        )

    def test_pending_payment_is_never_notified(self):
        client = FakeMaxClient()
        self.assertFalse(notify_customer_paid(payment=self._payment(Payment.Status.PENDING), client=client))
        self.assertEqual(client.sent, [])

    def test_non_max_order_is_never_notified(self):
        self.identity.channel = ChannelIdentity.Channel.TELEGRAM
        self.identity.save(update_fields=["channel"])
        client = FakeMaxClient()
        self.assertFalse(notify_customer_paid(payment=self._payment(), client=client))
        self.assertEqual(client.sent, [])

    def test_sends_once_and_records_evidence(self):
        payment = self._payment()
        client = FakeMaxClient()
        self.assertTrue(notify_customer_paid(payment=payment, client=client))
        self.assertFalse(notify_customer_paid(payment=payment, client=client))
        self.assertEqual(len(client.sent), 1)
        payment.refresh_from_db()
        notice = payment.metadata[PAID_NOTICE_KEY]
        self.assertEqual(notice["status"], "sent")
        self.assertTrue(notice["claimed_at"])
        self.assertTrue(notice["finished_at"])

    def test_unexpected_error_is_swallowed(self):
        payment = self._payment()
        with patch("apps.max_bot.paid_notice._claim", side_effect=RuntimeError("boom")), self.assertLogs(
            "apps.max_bot.paid_notice", level="ERROR"
        ):
            self.assertFalse(notify_customer_paid(payment=payment, client=FakeMaxClient()))
