import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.max_bot.adapter import MaxAdapter
from apps.core.tests_photo_gate import good_photo_bytes


class MaxAdapterTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(code="stickers", name="Sticker Pack")
        self.style = Style.objects.create(code="classic", name="Classic")
        self.adapter = MaxAdapter()
        self.max_user = {
            "user_id": 7001,
            "first_name": "Ivan",
            "last_name": "Petrov",
            "username": "ivan",
        }

    def test_identity_is_idempotent(self):
        first = self.adapter.get_or_create_identity(self.max_user)
        second = self.adapter.get_or_create_identity(self.max_user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ChannelIdentity.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(first.channel, ChannelIdentity.Channel.MAX)

    def test_creates_same_core_order_shape_as_other_channels(self):
        identity = self.adapter.get_or_create_identity(self.max_user)
        order = self.adapter.create_or_get_order(
            identity=identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )

        self.assertEqual(order.user_id, identity.user_id)
        self.assertEqual(order.channel_identity_id, identity.id)
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

    def test_repeat_same_order_does_not_duplicate(self):
        identity = self.adapter.get_or_create_identity(self.max_user)
        first = self.adapter.create_or_get_order(
            identity=identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        second = self.adapter.create_or_get_order(
            identity=identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.count(), 1)

    def test_photo_uses_shared_media_layer_and_completes_order(self):
        identity = self.adapter.get_or_create_identity(self.max_user)
        order = self.adapter.create_or_get_order(
            identity=identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                photo = self.adapter.save_photo_bytes(
                    identity=identity,
                    content=good_photo_bytes(),
                    filename="portrait.jpg",
                    mime_type="image/jpeg",
                )
                self.assertEqual(photo.order_id, order.id)
                self.assertTrue((Path(media_root) / photo.storage_key).exists())

        completed = self.adapter.complete_photos(identity)
        self.assertEqual(completed.status, Order.Status.READY_FOR_CHECKOUT)
