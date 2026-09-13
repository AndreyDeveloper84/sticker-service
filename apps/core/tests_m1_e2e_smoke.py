import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.max_bot.adapter import MaxAdapter
from apps.telegram_bot.adapter import TelegramAdapter


class M1EndToEndSmokeTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(code="stickers", name="Sticker Pack")
        self.style = Style.objects.create(code="classic", name="Classic")

        self.telegram = TelegramAdapter()
        self.max = MaxAdapter()

        self.telegram_user = {
            "id": 1001,
            "first_name": "Anna",
            "last_name": "Telegram",
            "username": "anna_tg",
        }
        self.max_user = {
            "user_id": 2001,
            "first_name": "Boris",
            "last_name": "Max",
            "username": "boris_max",
        }

    def _complete_order(self, *, adapter, identity, filename):
        order = adapter.create_or_get_order(
            identity=identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        self.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

        photo = adapter.save_photo_bytes(
            identity=identity,
            content=b"source-photo-bytes",
            filename=filename,
            mime_type="image/jpeg",
        )
        self.assertEqual(photo.order_id, order.id)

        order = adapter.complete_photos(identity)
        self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

        return order, photo

    def test_telegram_and_max_reach_same_production_console(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                telegram_identity = self.telegram.get_or_create_identity(
                    self.telegram_user
                )
                max_identity = self.max.get_or_create_identity(self.max_user)

                telegram_order, telegram_photo = self._complete_order(
                    adapter=self.telegram,
                    identity=telegram_identity,
                    filename="telegram-source.jpg",
                )
                max_order, max_photo = self._complete_order(
                    adapter=self.max,
                    identity=max_identity,
                    filename="max-source.jpg",
                )

                self.assertEqual(Order.objects.count(), 2)

                self.assertEqual(
                    telegram_order.channel_identity.channel,
                    ChannelIdentity.Channel.TELEGRAM,
                )
                self.assertEqual(
                    max_order.channel_identity.channel,
                    ChannelIdentity.Channel.MAX,
                )

                self.assertTrue(
                    (Path(media_root) / telegram_photo.storage_key).exists()
                )
                self.assertTrue(
                    (Path(media_root) / max_photo.storage_key).exists()
                )

                staff = get_user_model().objects.create_superuser(
                    username="m1-operator",
                    email="m1@example.com",
                    password="test-password",
                )
                self.client.force_login(staff)

                response = self.client.get(
                    reverse("admin:core_order_changelist")
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f"Order #{telegram_order.pk}")
                self.assertContains(response, f"Order #{max_order.pk}")
                self.assertContains(response, "Telegram")
                self.assertContains(response, "MAX")

    def test_identity_state_and_channel_isolation(self):
        telegram_identity = self.telegram.get_or_create_identity(
            self.telegram_user
        )
        telegram_identity_repeat = self.telegram.get_or_create_identity(
            self.telegram_user
        )

        max_identity = self.max.get_or_create_identity(self.max_user)
        max_identity_repeat = self.max.get_or_create_identity(
            self.max_user
        )

        self.assertEqual(
            telegram_identity.pk,
            telegram_identity_repeat.pk,
        )
        self.assertEqual(
            max_identity.pk,
            max_identity_repeat.pk,
        )

        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(ChannelIdentity.objects.count(), 2)

        telegram_order = self.telegram.create_or_get_order(
            identity=telegram_identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )
        max_order = self.max.create_or_get_order(
            identity=max_identity,
            product_code=self.product.code,
            style_code=self.style.code,
        )

        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(
                order=telegram_order,
                to_status=Order.Status.DRAFT,
            )

        telegram_order.refresh_from_db()
        max_order.refresh_from_db()

        self.assertEqual(
            telegram_order.status,
            Order.Status.AWAITING_PHOTOS,
        )
        self.assertEqual(
            max_order.status,
            Order.Status.AWAITING_PHOTOS,
        )

        self.max.save_photo_bytes(
            identity=max_identity,
            content=b"max-channel-still-works",
            filename="max-after-telegram-error.jpg",
            mime_type="image/jpeg",
        )

        max_order = self.max.complete_photos(max_identity)

        self.assertEqual(
            max_order.status,
            Order.Status.READY_FOR_CHECKOUT,
        )

        telegram_order.refresh_from_db()

        self.assertEqual(
            telegram_order.status,
            Order.Status.AWAITING_PHOTOS,
        )
