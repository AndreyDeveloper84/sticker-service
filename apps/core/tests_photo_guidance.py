"""DRF-2090: photo recommendations on the upload step (both bots) and the
soft one-photo reminder on «Фото загружены». Texts only — no domain change:
one photo is still accepted."""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.customer_hints import PHOTO_GUIDANCE, SINGLE_PHOTO_REMINDER
from apps.core.models import Order, Product, Style
from apps.core.bot_menu import CONTACT_PROMPT
from apps.core.tests_photo_gate import good_photo_bytes

SINGLE_CONFIG = {
    "kind": "single", "quantity": 1, "emotion_count": 1,
    "emotions": [{"code": "hello", "label": "Привет"}], "price_minor": 10000, "price_stars": 100, "currency": "RUB",
}


class PhotoGuidanceTextTests(TestCase):
    def test_guidance_lists_the_owner_recommendations(self):
        for phrase in ("2–3", "лицо", "плечи", "повседневная одежда", "открытых плеч", "купальников",
                       "солнцезащитных очков", "фильтров", "нейтральный фон", "ракурс", "Фото загружены"):
            self.assertIn(phrase, PHOTO_GUIDANCE, phrase)
        self.assertLess(len(PHOTO_GUIDANCE), 400, "one short message")

    def test_reminder_is_soft(self):
        self.assertIn("2–3 фото", SINGLE_PHOTO_REMINDER)
        self.assertIn("продолжить", SINGLE_PHOTO_REMINDER)

    def test_both_bots_use_the_shared_guidance(self):
        from apps.max_bot import views as max_views
        from apps.telegram_bot import views as tg_views

        self.assertEqual(max_views.PHOTO_PROMPT, PHOTO_GUIDANCE)
        self.assertEqual(tg_views.PHOTO_PROMPT, PHOTO_GUIDANCE)


class MaxPhotoGuidanceFlowTests(TestCase):
    USER = {"user_id": 7501, "first_name": "Ivan"}
    CHAT_ID = 9501

    def setUp(self):
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)

    def _post(self, payload):
        return self.client.post("/max/webhook/", data=json.dumps(payload), content_type="application/json")

    def _callback(self, payload):
        return {"update_type": "message_callback",
                "callback": {"callback_id": "cb", "payload": payload, "user": self.USER},
                "message": {"recipient": {"chat_id": self.CHAT_ID}, "body": {"mid": "m"}}}

    def _photo(self, name):
        return {"update_type": "message_created",
                "message": {"sender": self.USER, "recipient": {"chat_id": self.CHAT_ID},
                            "body": {"mid": f"mid-{name}", "attachments": [{"type": "image", "payload": {"url": f"https://cdn.max.test/{name}.jpg"}}]}}}

    def _texts(self, client):
        return [call.kwargs.get("text") for call in client.send_message.call_args_list]

    def _to_photo_step(self, client):
        self._post(self._callback("product:single-sticker"))
        self._post(self._callback("style:single-sticker:comic"))
        client.reset_mock()
        self._post(self._callback("emotion:hello"))
        self.assertEqual(client.send_message.call_args.kwargs["text"], PHOTO_GUIDANCE)
        client.reset_mock()

    def test_one_photo_gets_soft_reminder_then_consent(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.download_photo", return_value=good_photo_bytes()
        ):
            client = client_cls.return_value
            self._to_photo_step(client)
            self._post(self._photo("one"))
            self.assertEqual(client.send_message.call_args.kwargs["text"], "Фото сохранено. Отправьте ещё или нажмите «Фото загружены».")
            client.reset_mock()

            response = self._post(self._callback("photos_done"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._texts(client), [SINGLE_PHOTO_REMINDER, CONTACT_PROMPT])
            self.assertEqual(client.send_message.call_args.kwargs["buttons"][-1][0]["payload"], "menu:main")
            self.assertEqual(Order.objects.get().status, Order.Status.AWAITING_PHOTOS, "reminder never blocks")

    def test_two_photos_get_no_reminder(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls, mock.patch(
            "apps.max_bot.views.download_photo", return_value=good_photo_bytes()
        ):
            client = client_cls.return_value
            self._to_photo_step(client)
            self._post(self._photo("one"))
            self._post(self._photo("two"))
            client.reset_mock()

            self._post(self._callback("photos_done"))

            self.assertEqual(self._texts(client), [CONTACT_PROMPT])


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramPhotoGuidanceFlowTests(TestCase):
    USER = {"id": 3501, "first_name": "Olga"}
    CHAT_ID = 4501

    def setUp(self):
        Product.objects.create(code="single-sticker", name="Один стикер", config=SINGLE_CONFIG)
        Style.objects.create(code="comic", name="Комикс")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)

    def _post(self, payload):
        return self.client.post("/telegram/webhook/", data=json.dumps(payload), content_type="application/json")

    def _callback(self, data):
        return {"callback_query": {"id": "cb", "from": self.USER, "message": {"chat": {"id": self.CHAT_ID}}, "data": data}}

    def _photo(self, name):
        return {"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, "photo": [{"file_id": name}]}}

    def _texts(self, client):
        return [call.kwargs.get("text") for call in client.send_message.call_args_list]

    def _to_photo_step(self, client):
        client.get_file.side_effect = lambda file_id: {"file_path": f"photos/{file_id}.jpg"}
        client.download_file.return_value = good_photo_bytes()
        self._post(self._callback("product:single-sticker"))
        self._post(self._callback("style:single-sticker:comic"))
        client.reset_mock()
        self._post(self._callback("emotion:hello"))
        self.assertEqual(client.send_message.call_args.kwargs["text"], PHOTO_GUIDANCE)
        client.reset_mock()

    def test_one_photo_gets_soft_reminder_then_consent(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            self._to_photo_step(client)
            self._post(self._photo("one"))
            client.reset_mock()

            response = self._post(self._callback("photos_done"))

            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._texts(client), [SINGLE_PHOTO_REMINDER, CONTACT_PROMPT])
            self.assertEqual(
                client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"][-1][0]["callback_data"],
                "menu:main",
            )
            self.assertEqual(Order.objects.get().status, Order.Status.AWAITING_PHOTOS)

    def test_two_photos_get_no_reminder(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            self._to_photo_step(client)
            self._post(self._photo("one"))
            self._post(self._photo("two"))
            client.reset_mock()

            self._post(self._callback("photos_done"))

            self.assertEqual(self._texts(client), [CONTACT_PROMPT])
