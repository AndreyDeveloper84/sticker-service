from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings

from apps.core.models import Order, Product, Style
from apps.telegram_bot.adapter import TelegramAdapter, TelegramFlowError
from apps.core.tests_photo_gate import good_photo_bytes


class TelegramMediaFlowTests(TestCase):
    def setUp(self):
        self.adapter = TelegramAdapter()
        self.identity = self.adapter.get_or_create_identity({"id": 2001})
        product = Product.objects.create(code="stickers", name="Sticker Pack")
        style = Style.objects.create(code="comic", name="Comic")
        self.adapter.create_or_get_order(
            identity=self.identity,
            product_code=product.code,
            style_code=style.code,
        )

    def test_photo_is_saved_and_order_can_be_completed(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            photo = self.adapter.save_photo_bytes(
                identity=self.identity,
                content=good_photo_bytes(),
                filename="source.jpg",
                mime_type="image/jpeg",
            )
            self.assertEqual(photo.order.channel_identity_id, self.identity.id)
            order = self.adapter.complete_photos(self.identity)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)

    def test_completion_requires_photo(self):
        with self.assertRaises(TelegramFlowError):
            self.adapter.complete_photos(self.identity)
