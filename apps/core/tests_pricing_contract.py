"""DRF-2057: channel/provider-specific pricing contract.

Product.config["price_minor"] + "currency" is the RUB price (MAX/YooKassa).
Product.config["price_stars"] is the Telegram Stars (XTR) price.
The two are independent: no conversion, no fallback in either direction.

XTR values here are arbitrary test fixtures deliberately unequal to the RUB
amounts; they are not commercial prices.
"""

import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.core.models import ChannelIdentity, Order, Payment, Product, Style, User
from apps.max_bot.payments import CheckoutSession, MaxExternalPaymentAdapter
from apps.telegram_bot.adapter import TelegramAdapter
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter, configured_stars_price
from apps.telegram_bot.views import _summary_stars_price, _summary_text

PACK_TEST_STARS = 777
SINGLE_TEST_STARS = 333

TELEGRAM_USER = {"id": 5701, "username": "stars_buyer", "first_name": "Buyer"}

INVALID_STARS_VALUES = [0, -1, "500", "abc", 500.0, 12.5, True, False, [], {}]


def _seed_pilot(pack_stars=PACK_TEST_STARS, single_stars=SINGLE_TEST_STARS):
    call_command("seed_live_test", stdout=StringIO())
    pack = Product.objects.get(code="sticker-pack-9")
    single = Product.objects.get(code="single-sticker")
    pack.config = {**pack.config, "price_stars": pack_stars}
    single.config = {**single.config, "price_stars": single_stars}
    pack.save(update_fields=["config"])
    single.save(update_fields=["config"])
    return pack, single


def _set_config(product, **changes):
    config = dict(product.config)
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    product.config = config
    product.save(update_fields=["config"])


class FakeMaxProvider:
    name = "external-test"

    def create_checkout(self, *, payment):
        return CheckoutSession(checkout_url=f"https://pay.example.test/{payment.pk}", provider_reference=f"s-{payment.pk}")

    def parse_webhook(self, *, body, signature):
        return None


class ConfiguredStarsPriceTests(TestCase):
    def setUp(self):
        self.pack, self.single = _seed_pilot()

    def test_pilot_products_keep_rub_and_independent_stars(self):
        self.assertEqual((self.pack.config["price_minor"], self.pack.config["currency"]), (50000, "RUB"))
        self.assertEqual(configured_stars_price(self.pack), PACK_TEST_STARS)
        self.assertEqual((self.single.config["price_minor"], self.single.config["currency"]), (10000, "RUB"))
        self.assertEqual(configured_stars_price(self.single), SINGLE_TEST_STARS)

    def test_missing_stars_does_not_fall_back_to_rub(self):
        _set_config(self.pack, price_stars=None)
        self.assertEqual(self.pack.config["price_minor"], 50000)
        with self.assertRaisesMessage(TelegramPaymentError, "not configured"):
            configured_stars_price(self.pack)

    def test_invalid_stars_values_fail_closed(self):
        for value in INVALID_STARS_VALUES:
            with self.subTest(value=value):
                _set_config(self.pack, price_stars=value)
                with self.assertRaises(TelegramPaymentError):
                    configured_stars_price(self.pack)

    def test_blocked_checkout_is_logged_with_product_code_only(self):
        _set_config(self.pack, price_stars="500")
        with self.assertLogs("apps.telegram_bot.payments", level="WARNING") as logs:
            with self.assertRaises(TelegramPaymentError):
                configured_stars_price(self.pack)
        self.assertIn("product=sticker-pack-9", logs.output[0])
        self.assertIn("price_stars is invalid", logs.output[0])
        self.assertNotIn("generation_prompt", logs.output[0])


class TelegramStarsCheckoutPricingTests(TestCase):
    def setUp(self):
        self.pack, self.single = _seed_pilot()
        self.style = Style.objects.get(code="comic")
        self.identity = TelegramAdapter().get_or_create_identity(TELEGRAM_USER)
        self.payments = TelegramStarsPaymentAdapter()

    def _order(self, product):
        return Order.objects.create(
            user=self.identity.user,
            channel_identity=self.identity,
            product=product,
            style=self.style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )

    def test_pack_payment_amount_is_explicit_stars_in_xtr(self):
        self._order(self.pack)
        payment = self.payments.payment_for_identity(self.identity)
        self.assertEqual(payment.currency, "XTR")
        self.assertEqual(payment.amount_minor, PACK_TEST_STARS)

    def test_single_payment_amount_is_explicit_stars_in_xtr(self):
        self._order(self.single)
        payment = self.payments.payment_for_identity(self.identity)
        self.assertEqual(payment.currency, "XTR")
        self.assertEqual(payment.amount_minor, SINGLE_TEST_STARS)

    def test_changing_rub_price_does_not_change_stars_amount(self):
        _set_config(self.pack, price_minor=99900)
        self._order(self.pack)
        payment = self.payments.payment_for_identity(self.identity)
        self.assertEqual(payment.amount_minor, PACK_TEST_STARS)
        self.assertEqual(payment.currency, "XTR")

    def test_missing_stars_fails_before_payment_and_keeps_order_state(self):
        _set_config(self.pack, price_stars=None)
        order = self._order(self.pack)
        with self.assertRaises(TelegramPaymentError):
            self.payments.payment_for_identity(self.identity)
        self.assertFalse(Payment.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_invalid_stars_fails_before_payment_and_keeps_order_state(self):
        order = self._order(self.single)
        for value in INVALID_STARS_VALUES:
            with self.subTest(value=value):
                _set_config(self.single, price_stars=value)
                with self.assertRaises(TelegramPaymentError):
                    self.payments.payment_for_identity(self.identity)
                self.assertFalse(Payment.objects.exists())
                order.refresh_from_db()
                self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_summary_shows_invoice_stars_never_rub(self):
        order = self._order(self.pack)
        summary = {"product_name": "Pack", "style_name": "Comic", "quantity": 9, "emotions": []}
        text = _summary_text(summary, price_stars=_summary_stars_price(order.product))
        self.assertIn(f"Цена: {PACK_TEST_STARS} Stars", text)
        self.assertNotIn("500", text)
        self.assertNotIn("₽", text)

        _set_config(self.pack, price_stars="777")
        order.refresh_from_db()
        text = _summary_text(summary, price_stars=_summary_stars_price(order.product))
        self.assertNotIn("Цена", text)


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramInvoiceWebhookPricingTests(TestCase):
    def setUp(self):
        self.pack, self.single = _seed_pilot()
        identity = TelegramAdapter().get_or_create_identity(TELEGRAM_USER)
        self.order = Order.objects.create(
            user=identity.user,
            channel_identity=identity,
            product=self.pack,
            style=Style.objects.get(code="comic"),
            status=Order.Status.READY_FOR_CHECKOUT,
        )

    def _pay(self):
        update = {"callback_query": {"id": "cb-pay", "from": TELEGRAM_USER, "message": {"chat": {"id": 9901}}, "data": "pay"}}
        return self.client.post("/telegram/webhook/", data=json.dumps(update), content_type="application/json")

    def test_invoice_is_xtr_with_explicit_stars_and_no_provider_token(self):
        with mock.patch("apps.telegram_bot.client.TelegramBotClient._post", return_value={}) as post:
            response = self._pay()
        self.assertEqual(response.status_code, 200)
        method, body = post.call_args_list[0].args
        self.assertEqual(method, "sendInvoice")
        self.assertEqual(body["currency"], "XTR")
        self.assertEqual([price["amount"] for price in body["prices"]], [PACK_TEST_STARS])
        self.assertNotIn("provider_token", body)

    def test_missing_stars_sends_no_invoice(self):
        _set_config(self.pack, price_stars=None)
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            response = self._pay()
        self.assertFalse(response.json()["ok"])
        client_cls.return_value.send_invoice.assert_not_called()
        self.assertFalse(Payment.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_invalid_stars_sends_no_invoice(self):
        _set_config(self.pack, price_stars=50000.0)
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            response = self._pay()
        self.assertFalse(response.json()["ok"])
        client_cls.return_value.send_invoice.assert_not_called()
        self.assertFalse(Payment.objects.exists())


class MaxRubPricingTests(TestCase):
    def setUp(self):
        self.pack, self.single = _seed_pilot()
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-5701")
        self.style = Style.objects.get(code="comic")
        self.adapter = MaxExternalPaymentAdapter(provider=FakeMaxProvider())

    def _order(self, product):
        return Order.objects.create(
            user=self.identity.user,
            channel_identity=self.identity,
            product=product,
            style=self.style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )

    def test_max_uses_rub_price_minor_not_stars(self):
        for product, rub_minor in [(self.pack, 50000), (self.single, 10000)]:
            with self.subTest(product=product.code):
                Payment.objects.all().delete()
                Order.objects.all().delete()
                self._order(product)
                payment, _ = self.adapter.create_checkout(identity=self.identity)
                self.assertEqual(payment.amount_minor, rub_minor)
                self.assertEqual(payment.currency, "RUB")

    def test_changing_stars_price_does_not_change_rub_amount(self):
        _set_config(self.pack, price_stars=1)
        self._order(self.pack)
        payment, _ = self.adapter.create_checkout(identity=self.identity)
        self.assertEqual((payment.amount_minor, payment.currency), (50000, "RUB"))

    def test_max_checkout_works_without_stars_price(self):
        _set_config(self.single, price_stars=None)
        self._order(self.single)
        payment, _ = self.adapter.create_checkout(identity=self.identity)
        self.assertEqual((payment.amount_minor, payment.currency), (10000, "RUB"))


class PilotSeedPricingTests(TestCase):
    def test_seed_is_idempotent_and_prices_are_explicit_per_channel(self):
        call_command("seed_live_test", stdout=StringIO())
        first = {p.code: p.config for p in Product.objects.filter(is_active=True)}
        call_command("seed_live_test", stdout=StringIO())
        second = {p.code: p.config for p in Product.objects.filter(is_active=True)}

        self.assertEqual(first, second)
        self.assertEqual(Product.objects.count(), 2)
        expected = {"sticker-pack-9": (9, 9, 50000), "single-sticker": (1, 1, 10000)}
        for code, (quantity, emotion_count, rub_minor) in expected.items():
            with self.subTest(code=code):
                config = second[code]
                self.assertEqual((config["quantity"], config["emotion_count"]), (quantity, emotion_count))
                self.assertEqual(len(config["emotions"]), 9)
                self.assertEqual((config["price_minor"], config["currency"]), (rub_minor, "RUB"))
                # price_stars is its own explicit key and passes the strict XTR contract.
                self.assertIn("price_stars", config)
                self.assertEqual(configured_stars_price(Product.objects.get(code=code)), config["price_stars"])
