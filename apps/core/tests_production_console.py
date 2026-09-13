import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.media import MediaService


class ProductionConsoleTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(
            username="operator",
            email="operator@example.com",
            password="test-password",
        )
        self.client.force_login(self.staff)
        self.product = Product.objects.create(code="stickers", name="Sticker Pack")
        self.style = Style.objects.create(code="classic", name="Classic")

    def _order(self, *, channel, external_user_id, username):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=channel,
            external_user_id=external_user_id,
            username=username,
            display_name=username,
        )
        return Order.objects.create(
            user=user,
            channel_identity=identity,
            product=self.product,
            style=self.style,
            status=Order.Status.AWAITING_PHOTOS,
        )

    def test_queue_contains_telegram_and_max_orders_and_channel_filter(self):
        telegram_order = self._order(
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-1",
            username="telegram-user",
        )
        max_order = self._order(
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-1",
            username="max-user",
        )

        response = self.client.get(reverse("admin:core_order_changelist"))
        self.assertContains(response, f"Order #{telegram_order.pk}")
        self.assertContains(response, f"Order #{max_order.pk}")
        self.assertContains(response, "Telegram")
        self.assertContains(response, "MAX")

        response = self.client.get(
            reverse("admin:core_order_changelist"),
            {"channel_identity__channel__exact": ChannelIdentity.Channel.MAX},
        )
        self.assertNotContains(response, f"Order #{telegram_order.pk}")
        self.assertContains(response, f"Order #{max_order.pk}")

    def test_order_card_shows_source_photo_and_controlled_file_link(self):
        order = self._order(
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-2",
            username="photo-user",
        )
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                photo = MediaService().save_order_photo(
                    order=order,
                    file=SimpleUploadedFile(
                        "portrait.jpg",
                        b"image-bytes",
                        content_type="image/jpeg",
                    ),
                )
                response = self.client.get(
                    reverse("admin:core_order_change", args=[order.pk])
                )
                self.assertContains(response, "portrait.jpg")
                self.assertContains(
                    response,
                    reverse("admin:core_orderphoto_file", args=[photo.pk]),
                )

    def test_operator_can_edit_notes_without_changing_core_order_fields(self):
        order = self._order(
            channel=ChannelIdentity.Channel.MAX,
            external_user_id="max-2",
            username="notes-user",
        )
        original_status = order.status

        response = self.client.post(
            reverse("admin:core_order_change", args=[order.pk]),
            {
                "operator_notes": "Проверить качество исходных фото",
                "photos-TOTAL_FORMS": "0",
                "photos-INITIAL_FORMS": "0",
                "photos-MIN_NUM_FORMS": "0",
                "photos-MAX_NUM_FORMS": "0",
                "_save": "Сохранить",
            },
        )
        self.assertEqual(response.status_code, 302)

        order.refresh_from_db()
        self.assertEqual(order.operator_notes, "Проверить качество исходных фото")
        self.assertEqual(order.status, original_status)
