"""PAID confirmation to the Telegram customer after ``successful_payment``.

Best-effort and at most once (DRF-2077 follow-up): the first confirmed
payment sends exactly one message; a redelivered identical update, tampered
amounts and Telegram send failures never produce a second message, never
undo the payment and never turn the webhook answer into a non-2xx.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION
from apps.telegram_bot.client import TelegramAPIError
from apps.telegram_bot.paid_notice import PAID_NOTICE_KEY, PAID_NOTICE_TEXT, notify_customer_paid
from apps.telegram_bot.payments import TelegramStarsPaymentAdapter

WEBHOOK_URL = "/telegram/webhook/"
USER = {"id": 5101, "first_name": "Pavel"}
CHAT_ID = 6101


class FakeTelegramClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent = []

    def send_message(self, *, chat_id, text, reply_markup=None):
        if self.fail:
            raise TelegramAPIError("sendMessage", description="network: ConnectError")
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": len(self.sent), "chat": {"id": chat_id}}

    def send_invoice(self, **kwargs):
        return {"message_id": 99}

    def answer_pre_checkout_query(self, **kwargs):
        return True


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class PaidNoticeWebhookTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id=str(USER["id"])
        )
        product = Product.objects.create(code="single-sticker", name="Один стикер", config={"price_stars": 100})
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
        self.payment = TelegramStarsPaymentAdapter().payment_for_identity(self.identity)
        self.tg = FakeTelegramClient()

    def _successful_payment(self, *, total_amount=100, currency="XTR", charge="charge-1"):
        return {
            "message": {
                "from": USER,
                "chat": {"id": CHAT_ID},
                "successful_payment": {
                    "currency": currency,
                    "total_amount": total_amount,
                    "invoice_payload": TelegramStarsPaymentAdapter.payload(self.payment),
                    "telegram_payment_charge_id": charge,
                },
            }
        }

    def _post(self, update, *, client=None):
        with patch("apps.telegram_bot.views.TelegramBotClient", return_value=client or self.tg):
            return self.client.post(WEBHOOK_URL, data=json.dumps(update), content_type="application/json")

    def _notice(self):
        self.payment.refresh_from_db()
        return (self.payment.metadata or {}).get(PAID_NOTICE_KEY) or {}

    def _paid_messages(self, client=None):
        return [m for m in (client or self.tg).sent if m["text"] == PAID_NOTICE_TEXT]

    def test_first_successful_payment_sends_exactly_one_confirmation(self):
        response = self._post(self._successful_payment())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self._paid_messages(), [{"chat_id": CHAT_ID, "text": PAID_NOTICE_TEXT}])
        notice = self._notice()
        self.assertEqual(notice["status"], "sent")
        self.assertEqual(notice["message_id"], "1")

    def test_redelivered_update_does_not_send_twice(self):
        first = self._post(self._successful_payment())
        second = self._post(self._successful_payment())

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CONFIRMED).count(), 1)
        self.assertEqual(len(self._paid_messages()), 1)

    def test_send_failure_keeps_payment_confirmed_and_answers_200(self):
        failing = FakeTelegramClient(fail=True)

        with self.assertLogs("apps.telegram_bot.paid_notice", level="WARNING"):
            response = self._post(self._successful_payment(), client=failing)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        notice = self._notice()
        self.assertEqual(notice["status"], "failed")
        self.assertEqual(notice["error"], "TelegramAPIError")

        # a redelivery does not retry: at most one customer message
        replay = self._post(self._successful_payment())
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self._paid_messages(), [])

    def test_tampered_amount_sends_nothing(self):
        response = self._post(self._successful_payment(total_amount=1))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self._paid_messages(), [])
        self.assertEqual(self._notice(), {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_wrong_currency_sends_nothing(self):
        response = self._post(self._successful_payment(currency="RUB"))

        self.assertFalse(response.json()["ok"])
        self.assertEqual(self._paid_messages(), [])


class PaidNoticeUnitTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="tg-paid-unit"
        )
        product = Product.objects.create(code="p", name="P", config={"price_stars": 100})
        style = Style.objects.create(code="s", name="S")
        self.order = Order.objects.create(
            user=user, channel_identity=self.identity, product=product, style=style, status=Order.Status.PAID
        )

    def _payment(self, status=Payment.Status.CONFIRMED):
        return Payment.objects.create(
            order=self.order, provider="telegram_stars", amount_minor=100, currency="XTR", status=status,
            external_payment_id="charge-1" if status == Payment.Status.CONFIRMED else "",
        )

    def test_pending_payment_is_never_notified(self):
        client = FakeTelegramClient()
        self.assertFalse(notify_customer_paid(payment=self._payment(Payment.Status.PENDING), client=client, chat_id=1))
        self.assertEqual(client.sent, [])

    def test_non_telegram_order_is_never_notified(self):
        self.identity.channel = ChannelIdentity.Channel.MAX
        self.identity.save(update_fields=["channel"])
        client = FakeTelegramClient()
        self.assertFalse(notify_customer_paid(payment=self._payment(), client=client, chat_id=1))
        self.assertEqual(client.sent, [])

    def test_sends_once_and_records_evidence(self):
        payment = self._payment()
        client = FakeTelegramClient()
        self.assertTrue(notify_customer_paid(payment=payment, client=client, chat_id=7))
        self.assertFalse(notify_customer_paid(payment=payment, client=client, chat_id=7))
        self.assertEqual(client.sent, [{"chat_id": 7, "text": PAID_NOTICE_TEXT}])
        payment.refresh_from_db()
        notice = payment.metadata[PAID_NOTICE_KEY]
        self.assertEqual(notice["status"], "sent")
        self.assertEqual(notice["message_id"], "1")
        self.assertTrue(notice["claimed_at"])
        self.assertTrue(notice["finished_at"])

    def test_unexpected_error_is_swallowed(self):
        payment = self._payment()
        with patch("apps.telegram_bot.paid_notice._claim", side_effect=RuntimeError("boom")), self.assertLogs(
            "apps.telegram_bot.paid_notice", level="ERROR"
        ):
            self.assertFalse(notify_customer_paid(payment=payment, client=FakeTelegramClient(), chat_id=1))
