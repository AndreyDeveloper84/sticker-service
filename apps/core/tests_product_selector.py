"""DRF-2050: pilot product selector domain contract.

Product.config is the single source of quantity, emotion_count, the
deterministic emotion catalog and price; the flow stores the selected
emotion codes in Order.selection and blocks checkout until the selection
matches the product requirements.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.channel_order_flow import (
    ChannelFlowError,
    ChannelOrderFlowService,
    order_emotion_codes,
    product_emotion_count,
    product_emotion_options,
)

EMOTIONS = [
    {"code": "hello", "label": "Привет"},
    {"code": "bye", "label": "Пока"},
    {"code": "thanks", "label": "Спасибо"},
]

PACK_CONFIG = {
    "kind": "pack",
    "quantity": 9,
    "emotion_count": 3,  # trimmed set for tests; production seed ships 9
    "emotions": EMOTIONS,
    "price_minor": 50000,
    "price_stars": 500,
    "currency": "RUB",
}

SINGLE_CONFIG = {
    "kind": "single",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": EMOTIONS,
    "price_minor": 10000,
    "price_stars": 100,
    "currency": "RUB",
}


class ProductConfigContractTests(TestCase):
    def test_emotion_count_and_options_come_from_config(self):
        product = Product.objects.create(code="pack", name="Pack", config=PACK_CONFIG)
        self.assertEqual(product_emotion_count(product), 3)
        self.assertEqual(
            product_emotion_options(product),
            [{"code": "hello", "label": "Привет"}, {"code": "bye", "label": "Пока"}, {"code": "thanks", "label": "Спасибо"}],
        )

    def test_product_without_emotion_config_has_no_emotion_step(self):
        product = Product.objects.create(code="legacy", name="Legacy")
        self.assertEqual(product_emotion_count(product), 0)
        self.assertEqual(product_emotion_options(product), [])

    def test_label_falls_back_to_code(self):
        product = Product.objects.create(
            code="bare", name="Bare", config={"emotion_count": 1, "emotions": [{"code": "wow"}]}
        )
        self.assertEqual(product_emotion_options(product), [{"code": "wow", "label": "wow"}])


class EmotionSelectionTests(TestCase):
    def setUp(self):
        self.flow = ChannelOrderFlowService()
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="sel-1"
        )
        self.style = Style.objects.create(code="comic", name="Comic")
        self.pack = Product.objects.create(code="pack", name="Pack", config=PACK_CONFIG)
        self.single = Product.objects.create(code="single", name="Single", config=SINGLE_CONFIG)

    def _order(self, product):
        return self.flow.create_or_get_order(
            identity=self.identity, product_code=product.code, style_code=self.style.code
        )

    def test_select_emotion_stores_code_in_order_selection(self):
        order = self._order(self.single)
        self.assertEqual(order.selection, {"emotions": []})
        order = self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        self.assertEqual(order_emotion_codes(order), ["hello"])
        order.refresh_from_db()
        self.assertEqual(order.selection, {"emotions": ["hello"]})
        self.assertTrue(self.flow.selection_complete(order))

    def test_select_emotion_rejects_unknown_code(self):
        self._order(self.single)
        with self.assertRaises(ChannelFlowError):
            self.flow.select_emotion(identity=self.identity, emotion_code="nope")

    def test_select_emotion_rejects_duplicates_and_overflow(self):
        order = self._order(self.pack)
        self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        with self.assertRaises(ChannelFlowError):
            self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        self.flow.select_emotion(identity=self.identity, emotion_code="bye")
        self.flow.select_emotion(identity=self.identity, emotion_code="thanks")
        with self.assertRaises(ChannelFlowError):
            self.flow.select_emotion(identity=self.identity, emotion_code="hello")
        order.refresh_from_db()
        self.assertEqual(len(order_emotion_codes(order)), 3)

    def test_confirm_emotions_accepts_full_deterministic_set(self):
        order = self._order(self.pack)
        order = self.flow.confirm_emotions(identity=self.identity)
        self.assertEqual(order_emotion_codes(order), ["hello", "bye", "thanks"])
        self.assertTrue(self.flow.selection_complete(order))

    def test_confirm_emotions_rejects_misconfigured_product(self):
        broken = Product.objects.create(
            code="broken",
            name="Broken",
            config={"emotion_count": 2, "emotions": EMOTIONS},
        )
        self._order(broken)
        with self.assertRaises(ChannelFlowError):
            self.flow.confirm_emotions(identity=self.identity)

    def test_emotion_selection_requires_open_order(self):
        with self.assertRaises(ChannelFlowError):
            self.flow.select_emotion(identity=self.identity, emotion_code="hello")

    def test_checkout_blocked_until_emotions_complete(self):
        order = self._order(self.pack)
        order.photos.create(storage_key=f"orders/{order.pk}/a.jpg", size_bytes=1)
        with self.assertRaises(ChannelFlowError):
            self.flow.complete_photos(self.identity)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

        self.flow.confirm_emotions(identity=self.identity)
        order = self.flow.complete_photos(self.identity)
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_product_without_emotion_config_keeps_legacy_checkout(self):
        legacy = Product.objects.create(code="legacy", name="Legacy")
        order = self._order(legacy)
        self.assertEqual(order.selection, {})
        order.photos.create(storage_key=f"orders/{order.pk}/a.jpg", size_bytes=1)
        order = self.flow.complete_photos(self.identity)
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)


class OrderSummaryTests(TestCase):
    def setUp(self):
        self.flow = ChannelOrderFlowService()
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="sum-1"
        )
        self.style = Style.objects.create(code="comic", name="Комикс")
        self.pack = Product.objects.create(code="pack", name="Стикерпак", config=PACK_CONFIG)

    def test_summary_exposes_product_quantity_emotions_and_prices(self):
        order = self.flow.create_or_get_order(
            identity=self.identity, product_code=self.pack.code, style_code=self.style.code
        )
        self.flow.confirm_emotions(identity=self.identity)
        order.refresh_from_db()
        summary = self.flow.order_summary(order)
        self.assertEqual(summary["product_code"], "pack")
        self.assertEqual(summary["product_name"], "Стикерпак")
        self.assertEqual(summary["style_name"], "Комикс")
        self.assertEqual(summary["quantity"], 9)
        self.assertEqual(summary["emotion_count"], 3)
        self.assertEqual(summary["emotion_codes"], ["hello", "bye", "thanks"])
        self.assertEqual(summary["emotions"], ["Привет", "Пока", "Спасибо"])
        self.assertEqual(summary["price_minor"], 50000)
        self.assertEqual(summary["price_stars"], 500)
        self.assertEqual(summary["currency"], "RUB")


class CustomPhraseSelectionTests(TestCase):
    def setUp(self):
        self.flow = ChannelOrderFlowService()
        self.identity = ChannelIdentity.objects.create(
            user=User.objects.create(), channel=ChannelIdentity.Channel.MAX, external_user_id="custom-1"
        )
        self.style = Style.objects.create(code="drawn", name="Рисованные")
        self.product = Product.objects.create(
            code="custom", name="С надписями",
            config={
                "quantity": 3, "emotion_count": 3, "requires_custom_phrases": True,
                "requires_customer_contact": True,
                "emotions": [{"code": "custom-1", "label": "Фраза 1"}, {"code": "custom-2", "label": "Фраза 2"}, {"code": "custom-3", "label": "Фраза 3"}],
                "price_minor": 80000, "price_stars": 736,
            },
        )

    def test_custom_phrases_fill_production_slots_and_contact_appears_in_summary(self):
        order = self.flow.create_or_get_order(identity=self.identity, product_code="custom", style_code="drawn")
        order.photos.create(storage_key=f"orders/{order.pk}/a.jpg", size_bytes=1)
        self.flow.photos_ready(self.identity)
        with self.assertRaises(ChannelFlowError):
            self.flow.save_custom_phrases(identity=self.identity, text="Первая\nВторая")
        order = self.flow.save_custom_phrases(identity=self.identity, text="Первая\nВторая\nТретья")
        self.assertEqual(order_emotion_codes(order), ["custom-1", "custom-2", "custom-3"])
        self.assertTrue(self.flow.selection_complete(order))
        order = self.flow.save_customer_contact(identity=self.identity, text="Анна, @anna")
        summary = self.flow.order_summary(order)
        self.assertEqual(summary["emotions"], ["Первая", "Вторая", "Третья"])
        self.assertEqual(summary["contact"], "Анна, @anna")


class PilotSeedTests(TestCase):
    def test_seed_is_idempotent_and_leaves_exactly_three_active_products(self):
        Product.objects.create(code="personal-sticker-pack", name="Old", is_active=True)
        for _ in range(2):
            call_command("seed_live_test", stdout=StringIO())

        active = list(Product.objects.filter(is_active=True).order_by("id"))
        self.assertEqual([product.code for product in active], ["sticker-pack-9-custom", "sticker-pack-9", "single-sticker"])
        self.assertTrue(Product.objects.filter(code="personal-sticker-pack", is_active=False).exists())

        custom, pack, single = active
        self.assertEqual(custom.config["kind"], "custom_pack")
        self.assertEqual(custom.config["quantity"], 9)
        self.assertTrue(custom.config["requires_custom_phrases"])
        self.assertEqual(custom.config["price_minor"], 80000)
        self.assertEqual(custom.config["price_stars"], 736)
        self.assertEqual(pack.config["kind"], "pack")
        self.assertEqual(pack.config["quantity"], 9)
        self.assertEqual(pack.config["emotion_count"], 9)
        self.assertEqual(len(pack.config["emotions"]), 9)
        self.assertEqual(len({item["code"] for item in pack.config["emotions"]}), 9)
        self.assertEqual(pack.config["price_minor"], 50000)
        # DRF-2057: XTR price is approved independently of the RUB price.
        self.assertEqual(pack.config["price_stars"], 460)

        self.assertEqual(single.config["kind"], "single")
        self.assertEqual(single.config["quantity"], 1)
        self.assertEqual(single.config["emotion_count"], 1)
        self.assertEqual(single.config["price_minor"], 10000)
        self.assertEqual(single.config["price_stars"], 100)

        self.assertEqual(set(Style.objects.filter(is_active=True).values_list("code", flat=True)), {"3d", "drawn", "meme", "embroidery", "comic"})
