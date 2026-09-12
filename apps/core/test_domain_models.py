from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import ChannelIdentity, Order, OrderPhoto, Product, Style, User


class DomainModelTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(code="sticker-pack", name="Sticker Pack")
        self.style = Style.objects.create(code="classic", name="Classic")

    def test_one_user_can_have_telegram_and_max_identities(self):
        user = User.objects.create()
        telegram = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-123",
        )
        max_identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-456",
        )

        self.assertEqual(user.channel_identities.count(), 2)
        self.assertEqual(telegram.user_id, max_identity.user_id)

    def test_same_core_order_model_works_for_both_channels(self):
        telegram_user = User.objects.create()
        telegram_identity = ChannelIdentity.objects.create(
            user=telegram_user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-1",
        )
        max_user = User.objects.create()
        max_identity = ChannelIdentity.objects.create(
            user=max_user,
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-1",
        )

        telegram_order = Order.objects.create(
            user=telegram_user,
            channel_identity=telegram_identity,
            product=self.product,
            style=self.style,
        )
        max_order = Order.objects.create(
            user=max_user,
            channel_identity=max_identity,
            product=self.product,
            style=self.style,
        )

        self.assertEqual(telegram_order._meta.model, max_order._meta.model)
        self.assertEqual(telegram_order.status, Order.Status.DRAFT)
        self.assertEqual(max_order.status, Order.Status.DRAFT)

    def test_channel_external_user_is_unique(self):
        first_user = User.objects.create()
        second_user = User.objects.create()
        ChannelIdentity.objects.create(
            user=first_user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="same-id",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelIdentity.objects.create(
                    user=second_user,
                    channel=ChannelIdentity.Channel.TELEGRAM,
                    external_user_id="same-id",
                )

    def test_order_photo_stores_metadata_not_binary(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-photo",
        )
        order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=self.product,
            style=self.style,
        )
        photo = OrderPhoto.objects.create(
            order=order,
            storage_key="orders/1/source/photo-1.jpg",
            original_filename="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=12345,
        )

        self.assertEqual(photo.order_id, order.id)
        self.assertEqual(photo.storage_key, "orders/1/source/photo-1.jpg")
