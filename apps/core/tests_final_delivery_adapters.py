from django.test import SimpleTestCase

from apps.max_bot.final_delivery import MaxFinalDeliveryAdapter
from apps.telegram_bot.final_delivery import TelegramFinalDeliveryAdapter


class TelegramFinalDeliveryAdapterTests(SimpleTestCase):
    def test_sends_document_then_summary_message(self):
        class Client:
            def __init__(self):
                self.calls = []

            def send_document(self, **kwargs):
                self.calls.append(("sendDocument", kwargs))
                return {"message_id": 501, "chat": {"id": 100}}

            def send_photo(self, **kwargs):  # must NOT be used: re-encodes PNG
                raise AssertionError("final delivery must not use sendPhoto")

            def send_message(self, **kwargs):
                self.calls.append(("sendMessage", kwargs))
                return {"message_id": 502, "chat": {"id": 100}}

        client = Client()
        adapter = TelegramFinalDeliveryAdapter(client=client)
        self.assertEqual(adapter.channel, "telegram")

        item = adapter.send_final_item(
            recipient_id="100",
            content=b"png-bytes",
            mime_type="image/png",
            filename="sticker-2-bye.png",
            caption="Стикер 2/9 · Пока",
            index=2,
            total=9,
        )
        self.assertEqual(item.message_id, "501")
        self.assertEqual(item.metadata, {"chat_id": "100", "index": 2, "total": 9})

        summary = adapter.send_final_summary(recipient_id="100", text="Набор готов", total=9)
        self.assertEqual(summary.message_id, "502")

        self.assertEqual([name for name, _ in client.calls], ["sendDocument", "sendMessage"])
        document = client.calls[0][1]
        self.assertEqual(document["chat_id"], "100")
        self.assertEqual(document["content"], b"png-bytes")
        self.assertEqual(document["filename"], "sticker-2-bye.png")
        self.assertEqual(document["mime_type"], "image/png")
        self.assertEqual(document["caption"], "Стикер 2/9 · Пока")
        message = client.calls[1][1]
        self.assertEqual(message, {"chat_id": "100", "text": "Набор готов"})

    def test_empty_response_yields_empty_message_id(self):
        class Client:
            def send_document(self, **kwargs):
                return None

        item = TelegramFinalDeliveryAdapter(client=Client()).send_final_item(
            recipient_id="100",
            content=b"x",
            mime_type="image/png",
            filename="sticker-1-hello.png",
            caption="",
            index=1,
            total=1,
        )
        self.assertEqual(item.message_id, "")


class MaxFinalDeliveryAdapterTests(SimpleTestCase):
    def test_sends_image_then_summary_message(self):
        class Client:
            def __init__(self):
                self.calls = []

            def send_image(self, **kwargs):
                self.calls.append(("send_image", kwargs))
                return {"body": {"mid": "max-501"}}

            def send_message(self, **kwargs):
                self.calls.append(("send_message", kwargs))
                return {"message": {"mid": "max-502"}}

        client = Client()
        adapter = MaxFinalDeliveryAdapter(client=client)
        self.assertEqual(adapter.channel, "max")

        item = adapter.send_final_item(
            recipient_id="200",
            content=b"png-bytes",
            mime_type="image/png",
            filename="sticker-1-hello.png",
            caption="Стикер 1/1 · Привет",
            index=1,
            total=1,
        )
        self.assertEqual(item.message_id, "max-501")
        self.assertEqual(item.metadata, {"user_id": "200", "index": 1, "total": 1})

        summary = adapter.send_final_summary(recipient_id="200", text="Набор готов", total=1)
        self.assertEqual(summary.message_id, "max-502")

        self.assertEqual([name for name, _ in client.calls], ["send_image", "send_message"])
        image = client.calls[0][1]
        self.assertEqual(image["user_id"], "200")
        self.assertEqual(image["content"], b"png-bytes")
        self.assertEqual(image["filename"], "sticker-1-hello.png")
        self.assertEqual(image["mime_type"], "image/png")
        self.assertEqual(image["caption"], "Стикер 1/1 · Привет")
        self.assertEqual(client.calls[1][1], {"user_id": "200", "text": "Набор готов"})

    def test_real_max_envelope_message_body_mid(self):
        class Client:
            def send_image(self, **kwargs):
                return {"message": {"sender": {}, "recipient": {}, "body": {"mid": "mid.real-1", "seq": 9}}}

            def send_message(self, **kwargs):
                return {"message": {"body": {"mid": "mid.real-2", "seq": 10}}}

        adapter = MaxFinalDeliveryAdapter(client=Client())
        item = adapter.send_final_item(
            recipient_id="200", content=b"x", mime_type="image/png", filename="s.png", caption="c", index=1, total=1
        )
        self.assertEqual(item.message_id, "mid.real-1")
        summary = adapter.send_final_summary(recipient_id="200", text="t", total=1)
        self.assertEqual(summary.message_id, "mid.real-2")

    def test_top_level_mid_is_accepted(self):
        class Client:
            def send_image(self, **kwargs):
                return {"mid": "max-1"}

        item = MaxFinalDeliveryAdapter(client=Client()).send_final_item(
            recipient_id="200",
            content=b"x",
            mime_type="image/png",
            filename="sticker-1-hello.png",
            caption="",
            index=1,
            total=1,
        )
        self.assertEqual(item.message_id, "max-1")
