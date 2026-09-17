from django.test import SimpleTestCase

from apps.max_bot.preview_delivery import MaxPreviewDeliveryAdapter
from apps.telegram_bot.preview_delivery import TelegramPreviewDeliveryAdapter


class TelegramPreviewAdapterTests(SimpleTestCase):
    def test_returns_message_id_and_feedback_controls(self):
        class Client:
            def send_photo(self, **kwargs):
                return {"message_id": 77, "chat": {"id": 100}}

            def send_message(self, **kwargs):
                self.reply_markup = kwargs["reply_markup"]
                return {"message_id": 78}

        client = Client()
        result = TelegramPreviewDeliveryAdapter(client=client).send_preview(
            recipient_id="100",
            content=b"image",
            mime_type="image/png",
            filename="preview.png",
            caption="preview",
        )
        self.assertEqual(result.message_id, "77")
        self.assertEqual(result.metadata["controls_message_id"], "78")
        callbacks = [button["callback_data"] for button in client.reply_markup["inline_keyboard"][0]]
        self.assertEqual(callbacks, ["preview_approve", "preview_revision"])


class MaxPreviewAdapterTests(SimpleTestCase):
    def test_returns_message_id_and_feedback_controls(self):
        class Client:
            def send_image(self, **kwargs):
                return {"message": {"body": {"mid": "max-77", "seq": 7}}}

            def send_message(self, **kwargs):
                self.buttons = kwargs["buttons"]
                return {"body": {"mid": "max-78"}}

        client = Client()
        result = MaxPreviewDeliveryAdapter(client=client).send_preview(
            recipient_id="200",
            content=b"image",
            mime_type="image/png",
            filename="preview.png",
            caption="preview",
        )
        self.assertEqual(result.message_id, "max-77")
        self.assertEqual(result.metadata["controls_message_id"], "max-78")
        payloads = [button["payload"] for button in client.buttons[0]]
        self.assertEqual(payloads, ["preview_approve", "preview_revision"])
