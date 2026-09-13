from django.test import SimpleTestCase

from apps.max_bot.preview_delivery import MaxPreviewDeliveryAdapter
from apps.telegram_bot.preview_delivery import TelegramPreviewDeliveryAdapter


class TelegramPreviewAdapterTests(SimpleTestCase):
    def test_returns_message_id(self):
        class Client:
            def send_photo(self, **kwargs):
                return {"message_id": 77, "chat": {"id": 100}}

        result = TelegramPreviewDeliveryAdapter(client=Client()).send_preview(
            recipient_id="100",
            content=b"image",
            mime_type="image/png",
            filename="preview.png",
            caption="preview",
        )
        self.assertEqual(result.message_id, "77")
        self.assertEqual(result.metadata["chat_id"], "100")


class MaxPreviewAdapterTests(SimpleTestCase):
    def test_returns_message_id(self):
        class Client:
            def send_image(self, **kwargs):
                return {"body": {"mid": "max-77"}}

        result = MaxPreviewDeliveryAdapter(client=Client()).send_preview(
            recipient_id="200",
            content=b"image",
            mime_type="image/png",
            filename="preview.png",
            caption="preview",
        )
        self.assertEqual(result.message_id, "max-77")
        self.assertEqual(result.metadata["user_id"], "200")
