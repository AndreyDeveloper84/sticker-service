import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import TestCase
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION
from apps.max_bot.payments import MaxExternalPaymentAdapter, MaxPaymentError, MaxPaymentIgnored
from apps.max_bot.payments_yookassa import YooKassaPaymentProvider

WEBHOOK_URL = "/max/payment/webhook/"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeHttpClient:
    def __init__(self, *, created=None, payment_object=None, post_error=None, get_error=None):
        self.created = created or {}
        self.payment_object = payment_object or {}
        self.post_error = post_error
        self.get_error = get_error
        self.posts = []
        self.gets = []

    def post(self, url, *, json=None, auth=None, headers=None):
        self.posts.append({"url": url, "json": json, "auth": auth, "headers": headers})
        if self.post_error:
            raise self.post_error
        return FakeResponse(self.created)

    def get(self, url, *, auth=None):
        self.gets.append({"url": url, "auth": auth})
        if self.get_error:
            raise self.get_error
        return FakeResponse(self.payment_object)


def make_provider(http_client):
    return YooKassaPaymentProvider(
        shop_id="shop-1",
        secret_key="secret-1",
        return_url="https://return.example.test/done",
        http_client=http_client,
    )


def fake_payment(pk=7, order_id=3, amount_minor=10000, currency="RUB"):
    return SimpleNamespace(pk=pk, order_id=order_id, amount_minor=amount_minor, currency=currency)


def webhook_body(event="payment.succeeded", object_id="yk-tx-1"):
    return json.dumps({"event": event, "object": {"id": object_id}}).encode("utf-8")


def yk_payment_object(payment_id, *, status="succeeded", value="199.00", currency="RUB", object_id="yk-tx-1",
                      income_value=None, income_currency=None):
    data = {
        "id": object_id,
        "status": status,
        "amount": {"value": value, "currency": currency},
        "metadata": {"payment_id": str(payment_id), "order_id": "1"},
    }
    if income_value is not None:
        data["income_amount"] = {"value": income_value, "currency": income_currency or currency}
    return data


class YooKassaCreateCheckoutTests(TestCase):
    def test_create_checkout_sends_payload_and_maps_response(self):
        http = FakeHttpClient(created={
            "id": "yk-tx-1",
            "confirmation": {"type": "redirect", "confirmation_url": "https://pay.yookassa.test/abc"},
        })
        provider = make_provider(http)

        session = provider.create_checkout(payment=fake_payment())

        self.assertEqual(session.checkout_url, "https://pay.yookassa.test/abc")
        self.assertEqual(session.provider_reference, "yk-tx-1")
        call = http.posts[0]
        self.assertEqual(call["url"], "https://api.yookassa.ru/v3/payments")
        self.assertEqual(call["auth"], ("shop-1", "secret-1"))
        self.assertEqual(call["headers"]["Idempotence-Key"], "sticker-payment-7")
        self.assertEqual(call["json"]["amount"], {"value": "100.00", "currency": "RUB"})
        self.assertTrue(call["json"]["capture"])
        self.assertEqual(
            call["json"]["confirmation"],
            {"type": "redirect", "return_url": "https://return.example.test/done"},
        )
        self.assertEqual(call["json"]["description"], "Sticker order #3")
        self.assertEqual(call["json"]["metadata"], {"payment_id": "7", "order_id": "3"})

    def test_idempotence_key_is_stable_per_payment(self):
        http = FakeHttpClient(created={
            "id": "yk-tx-1",
            "confirmation": {"confirmation_url": "https://pay.yookassa.test/abc"},
        })
        provider = make_provider(http)

        provider.create_checkout(payment=fake_payment(pk=42))
        provider.create_checkout(payment=fake_payment(pk=42))

        keys = [call["headers"]["Idempotence-Key"] for call in http.posts]
        self.assertEqual(keys, ["sticker-payment-42", "sticker-payment-42"])

    def test_missing_confirmation_url_is_rejected(self):
        http = FakeHttpClient(created={"id": "yk-tx-1", "confirmation": {}})
        provider = make_provider(http)

        with self.assertRaises(MaxPaymentError):
            provider.create_checkout(payment=fake_payment())

    def test_non_https_confirmation_url_is_rejected(self):
        http = FakeHttpClient(created={
            "id": "yk-tx-1",
            "confirmation": {"confirmation_url": "http://pay.yookassa.test/abc"},
        })
        provider = make_provider(http)

        with self.assertRaises(MaxPaymentError):
            provider.create_checkout(payment=fake_payment())

    def test_http_error_is_wrapped(self):
        request = httpx.Request("POST", "https://api.yookassa.ru/v3/payments")
        error = httpx.HTTPStatusError(
            "bad request", request=request, response=httpx.Response(400, request=request)
        )
        provider = make_provider(FakeHttpClient(post_error=error))

        with self.assertRaises(MaxPaymentError):
            provider.create_checkout(payment=fake_payment())

    def test_timeout_is_wrapped(self):
        provider = make_provider(FakeHttpClient(post_error=httpx.TimeoutException("boom")))

        with self.assertRaises(MaxPaymentError):
            provider.create_checkout(payment=fake_payment())

    def test_from_env_requires_mandatory_variables(self):
        blank = {
            "YOOKASSA_SHOP_ID": "",
            "YOOKASSA_SECRET_KEY": "",
            "YOOKASSA_RETURN_URL": "",
            "YOOKASSA_API_BASE": "",
        }
        with patch.dict(os.environ, blank):
            with self.assertRaises(MaxPaymentError):
                YooKassaPaymentProvider.from_env()

    def test_from_env_reads_configuration(self):
        env = {
            "YOOKASSA_SHOP_ID": "shop-9",
            "YOOKASSA_SECRET_KEY": "secret-9",
            "YOOKASSA_RETURN_URL": "https://return.example.test/",
            "YOOKASSA_API_BASE": "https://yk-sandbox.example.test/v3/",
        }
        with patch.dict(os.environ, env):
            provider = YooKassaPaymentProvider.from_env()

        self.assertEqual(provider.name, "yookassa")
        self.assertEqual(provider.shop_id, "shop-9")
        self.assertEqual(provider.return_url, "https://return.example.test/")
        self.assertEqual(provider.api_base, "https://yk-sandbox.example.test/v3")


class YooKassaWebhookParsingTests(TestCase):
    def test_succeeded_webhook_fetches_and_maps_payment(self):
        http = FakeHttpClient(payment_object=yk_payment_object(7, value="100.00"))
        provider = make_provider(http)

        confirmation = provider.parse_webhook(body=webhook_body(), signature="")

        self.assertEqual(confirmation.payment_id, 7)
        self.assertEqual(confirmation.external_payment_id, "yk-tx-1")
        self.assertEqual(confirmation.status, "succeeded")
        self.assertEqual(confirmation.amount_minor, 10000)
        self.assertEqual(confirmation.currency, "RUB")
        self.assertEqual(http.gets[0]["url"], "https://api.yookassa.ru/v3/payments/yk-tx-1")
        self.assertEqual(http.gets[0]["auth"], ("shop-1", "secret-1"))

    # DRF-2111 PR-B: fee evidence from the provider's own income_amount.

    def test_income_amount_yields_provider_confirmed_fee(self):
        http = FakeHttpClient(payment_object=yk_payment_object(7, value="100.00", income_value="96.50"))
        confirmation = make_provider(http).parse_webhook(body=webhook_body(), signature="")
        fee = confirmation.metadata["fee"]
        self.assertEqual(fee["amount_minor"], 350)
        self.assertEqual(fee["income_amount_minor"], 9650)
        self.assertEqual(fee["currency"], "RUB")
        self.assertEqual(fee["source"], "PROVIDER_CONFIRMED")
        self.assertTrue(fee["captured_at"])
        self.assertEqual(confirmation.amount_minor, 10000)  # the charged amount is untouched

    def test_missing_income_amount_leaves_fee_unknown(self):
        http = FakeHttpClient(payment_object=yk_payment_object(7, value="100.00"))
        confirmation = make_provider(http).parse_webhook(body=webhook_body(), signature="")
        self.assertNotIn("fee", confirmation.metadata)

    def test_inconsistent_income_amount_is_not_trusted(self):
        for kwargs in ({"income_value": "120.00"}, {"income_value": "96.50", "income_currency": "USD"},
                       {"income_value": "abc"}):
            http = FakeHttpClient(payment_object=yk_payment_object(7, value="100.00", **kwargs))
            confirmation = make_provider(http).parse_webhook(body=webhook_body(), signature="")
            self.assertNotIn("fee", confirmation.metadata, kwargs)

    def test_canceled_status_is_propagated(self):
        http = FakeHttpClient(payment_object=yk_payment_object(7, status="canceled"))
        provider = make_provider(http)

        confirmation = provider.parse_webhook(body=webhook_body(), signature="")

        self.assertEqual(confirmation.status, "canceled")

    def test_malformed_json_is_rejected(self):
        provider = make_provider(FakeHttpClient())

        with self.assertRaises(MaxPaymentError):
            provider.parse_webhook(body=b"not-json", signature="")

    def test_webhook_without_payment_id_is_rejected(self):
        provider = make_provider(FakeHttpClient())

        with self.assertRaises(MaxPaymentError):
            provider.parse_webhook(body=b'{"event": "payment.succeeded"}', signature="")

    def test_missing_payment_id_metadata_fails_closed(self):
        fetched = yk_payment_object(7)
        fetched["metadata"] = {}
        provider = make_provider(FakeHttpClient(payment_object=fetched))

        with self.assertRaises(MaxPaymentError):
            provider.parse_webhook(body=webhook_body(), signature="")

    def test_provider_fetch_error_is_wrapped(self):
        provider = make_provider(FakeHttpClient(get_error=httpx.ConnectError("down")))

        with self.assertRaises(MaxPaymentError):
            provider.parse_webhook(body=webhook_body(), signature="")


class YooKassaAdapterTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-yk",
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
            consent_version=PILOT_CONSENT_VERSION,
            consent_accepted_at=timezone.now(),
        )
        self.http = FakeHttpClient(created={
            "id": "yk-tx-1",
            "confirmation": {"confirmation_url": "https://pay.yookassa.test/abc"},
        })
        self.provider = make_provider(self.http)
        self.adapter = MaxExternalPaymentAdapter(provider=self.provider)

    def start_payment(self):
        payment, _session = self.adapter.create_checkout(identity=self.identity)
        return payment

    def confirm(self, payment, **overrides):
        self.http.payment_object = yk_payment_object(payment.pk, **overrides)
        return self.adapter.confirm_webhook(body=webhook_body(), signature="")

    def test_succeeded_webhook_marks_order_paid_once(self):
        payment = self.start_payment()

        confirmed = self.confirm(payment)

        confirmed.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(confirmed.status, Payment.Status.CONFIRMED)
        self.assertEqual(confirmed.external_payment_id, "yk-tx-1")
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_confirmation_stores_fee_evidence_additively(self):
        payment = self.start_payment()

        confirmed = self.confirm(payment, income_value="192.03")

        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, Payment.Status.CONFIRMED)
        self.assertEqual(confirmed.amount_minor, 19900)
        self.assertEqual(confirmed.metadata["fee"]["amount_minor"], 697)
        self.assertEqual(confirmed.metadata["fee"]["source"], "PROVIDER_CONFIRMED")
        self.assertEqual(confirmed.metadata["channel"], "max")  # earlier metadata kept
        self.assertIn("provider_webhook", confirmed.metadata)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_confirmation_without_income_amount_has_no_fee(self):
        payment = self.start_payment()
        confirmed = self.confirm(payment)
        confirmed.refresh_from_db()
        self.assertNotIn("fee", confirmed.metadata)
        self.assertEqual(confirmed.status, Payment.Status.CONFIRMED)

    def test_duplicate_webhook_is_idempotent(self):
        payment = self.start_payment()

        first = self.confirm(payment)
        second = self.confirm(payment)

        self.assertEqual(first.pk, second.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CONFIRMED).count(), 1)

    def test_amount_mismatch_fails_closed(self):
        payment = self.start_payment()

        with self.assertRaises(MaxPaymentError):
            self.confirm(payment, value="9.00")

        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_currency_mismatch_fails_closed(self):
        payment = self.start_payment()

        with self.assertRaises(MaxPaymentError):
            self.confirm(payment, currency="USD")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_canceled_webhook_is_ignored_and_fails_closed(self):
        payment = self.start_payment()

        with self.assertRaises(MaxPaymentIgnored) as ctx:
            self.confirm(payment, status="canceled")

        self.assertEqual(ctx.exception.reason, "status canceled")
        self.order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_unknown_payment_is_ignored(self):
        self.http.payment_object = yk_payment_object(999999)

        with self.assertRaises(MaxPaymentIgnored) as ctx:
            self.adapter.confirm_webhook(body=webhook_body(), signature="")

        self.assertEqual(ctx.exception.reason, "unknown payment")


class YooKassaWebhookViewTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-yk-view",
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
            consent_version=PILOT_CONSENT_VERSION,
            consent_accepted_at=timezone.now(),
        )
        self.http = FakeHttpClient(created={
            "id": "yk-tx-1",
            "confirmation": {"confirmation_url": "https://pay.yookassa.test/abc"},
        })
        self.provider = make_provider(self.http)
        self.adapter = MaxExternalPaymentAdapter(provider=self.provider)
        self.payment, _session = self.adapter.create_checkout(identity=self.identity)

    def post_webhook(self, body):
        # The PAID customer notice (tests_paid_notice) must not hit the real
        # MAX API from these payment-contract tests.
        with patch(
            "apps.max_bot.provider_webhook.YooKassaPaymentProvider.from_env",
            return_value=self.provider,
        ), patch("apps.max_bot.provider_webhook.MaxBotClient"):
            return self.client.post(WEBHOOK_URL, data=body, content_type="application/json")

    def test_invalid_payload_returns_400(self):
        response = self.post_webhook(b"not-json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_succeeded_webhook_returns_200_and_marks_paid(self):
        self.http.payment_object = yk_payment_object(self.payment.pk)

        response = self.post_webhook(webhook_body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_duplicate_webhook_returns_200_and_stays_single_payment(self):
        self.http.payment_object = yk_payment_object(self.payment.pk)

        first = self.post_webhook(webhook_body())
        second = self.post_webhook(webhook_body())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Payment.objects.filter(status=Payment.Status.CONFIRMED).count(), 1)

    def test_unknown_payment_is_acked_as_ignored(self):
        self.http.payment_object = yk_payment_object(999999)

        response = self.post_webhook(webhook_body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": "unknown payment"})

    def test_canceled_status_is_acked_as_ignored(self):
        self.http.payment_object = yk_payment_object(self.payment.pk, status="canceled")

        response = self.post_webhook(webhook_body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": "status canceled"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_amount_mismatch_returns_400(self):
        self.http.payment_object = yk_payment_object(self.payment.pk, value="9.00")

        response = self.post_webhook(webhook_body())

        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
