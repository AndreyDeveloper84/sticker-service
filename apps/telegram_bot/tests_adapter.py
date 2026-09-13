from django.test import TestCase

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.telegram_bot.adapter import TelegramAdapter, TelegramFlowError


class TelegramIdentityTests(TestCase):
    def setUp(self):
        self.adapter = TelegramAdapter()
        self.user_data = {"id": 1001, "username": "andrey", "first_name": "Andrey"}

    def test_identity_is_idempotent(self):
        first = self.adapter.get_or_create_identity(self.user_data)
        second = self.adapter.get_or_create_identity(self.user_data)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ChannelIdentity.objects.count(), 1)


class TelegramOrderTests(TestCase):
    def setUp(self):
        self.adapter = TelegramAdapter()
        self.identity = self.adapter.get_or_create_identity({"id": 1002})
        self.product = Product.objects.create(code="stickers", name="Sticker Pack")
        self.style = Style.objects.create(code="comic", name="Comic")

    def test_order_creation_uses_core_order(self):
        order = self.adapter.create_or_get_order(
            identity=self.identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        self.assertEqual(order.user_id, self.identity.user_id)
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

    def test_same_selection_reuses_order(self):
        first = self.adapter.create_or_get_order(
            identity=self.identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        second = self.adapter.create_or_get_order(
            identity=self.identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.count(), 1)

    def test_inactive_product_is_rejected(self):
        self.product.is_active = False
        self.product.save(update_fields=["is_active"])
        with self.assertRaises(TelegramFlowError):
            self.adapter.create_or_get_order(
                identity=self.identity,
                product_code=self.product.code,
                style_code=self.style.code,
            )
