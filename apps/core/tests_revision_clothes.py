"""«Сменить одежду» (owner GO 2026-09-20): Revision.Category.CLOTHES, a button
on the revision step in both bots, optional free text «во что переодеть», a
clothing clause in the revision prompt, a Russian console label. The
one-included-revision rule is unchanged.
"""

import json
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.console_text import REVISION_CATEGORIES
from apps.core.customer_hints import (
    REVISION_ACCEPTED_TEXT,
    REVISION_CLOTHES_ACCEPTED_TEXT,
    REVISION_TEXT_SAVED_TEXT,
)
from apps.core.models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, Product, Revision, Style, User
from apps.core.services.generation_prompts import (
    REVISION_INSTRUCTIONS,
    REVISION_LEAD,
    render_revision_request,
)
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService


def make_preview_review_order(*, channel, external_user_id):
    """An order whose approved preview was delivered: the customer may now
    approve it or request the one included revision."""
    user = User.objects.create()
    identity = ChannelIdentity.objects.create(user=user, channel=channel, external_user_id=external_user_id)
    product = Product.objects.create(code="sticker-single", name="Single", config={"generation_prompt": "Make preview"})
    style = Style.objects.create(code="3d", name="3D", config={"prompt": "Use a polished 3D style."})
    order = Order.objects.create(
        user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PREVIEW_REVIEW
    )
    job = GenerationJob.objects.create(
        order=order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.SUCCEEDED,
        attempt=1, provider="fake",
    )
    GeneratedAsset.objects.create(
        order=order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key=f"orders/{order.pk}/preview.png",
        metadata={"internal_approved": True, "deliveries": [{"status": "sent", "channel": channel, "message_id": "1"}]},
    )
    return order, identity


class ClothesPromptTests(SimpleTestCase):
    def test_category_exists_with_clause_and_console_label(self):
        self.assertEqual(Revision.Category.CLOTHES, "clothes")
        self.assertIn("clothes", REVISION_INSTRUCTIONS)
        self.assertEqual(REVISION_CATEGORIES[Revision.Category.CLOTHES], "Сменить одежду")

    def test_clothes_without_text_neutral_outfit_keeps_face(self):
        text = render_revision_request(Revision.Category.CLOTHES, "")
        self.assertTrue(text.startswith(REVISION_LEAD))
        self.assertIn("replace the clothing", text)
        self.assertIn("neutral outfit", text)
        self.assertIn("keep the face, hairstyle, likeness and expression unchanged", text)
        self.assertNotIn("Revision category", text)

    def test_clothes_with_text_uses_the_customer_outfit(self):
        text = render_revision_request(Revision.Category.CLOTHES, " красный свитер ")
        self.assertIn("dress the person in the outfit the customer describes: «красный свитер»", text)
        self.assertIn("keep the face, hairstyle, likeness and expression unchanged", text)
        self.assertNotIn("neutral outfit", text)
        self.assertNotIn("own words", text)


class AttachRevisionTextServiceTests(TestCase):
    def setUp(self):
        self.order, self.identity = make_preview_review_order(channel="max", external_user_id="u1")

    def test_text_attaches_once_to_pending_clothes_revision(self):
        PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.CLOTHES)
        revision = PreviewFeedbackService.attach_revision_text(identity=self.identity, text="  белая рубашка ")
        self.assertEqual(revision.customer_text, "белая рубашка")
        # a second message is not a revision text any more
        self.assertIsNone(PreviewFeedbackService.attach_revision_text(identity=self.identity, text="ещё"))
        revision.refresh_from_db()
        self.assertEqual(revision.customer_text, "белая рубашка")

    def test_other_categories_and_no_revision_do_not_take_text(self):
        self.assertIsNone(PreviewFeedbackService.attach_revision_text(identity=self.identity, text="привет"))
        PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.FACE)
        self.assertIsNone(PreviewFeedbackService.attach_revision_text(identity=self.identity, text="привет"))
        self.assertEqual(Revision.objects.get(order=self.order).customer_text, "")

    def test_text_not_taken_once_generation_started(self):
        revision = PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.CLOTHES)
        revision.status = Revision.Status.GENERATING
        revision.save(update_fields=["status"])
        self.order.status = Order.Status.REVISION_GENERATING
        self.order.save(update_fields=["status"])
        self.assertIsNone(PreviewFeedbackService.attach_revision_text(identity=self.identity, text="поздно"))

    def test_one_included_revision_rule_unchanged(self):
        PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.CLOTHES)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REVISION_REQUESTED)
        # repeated request while pending is idempotent; after completion → 409-class error
        PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.FACE)
        self.assertEqual(Revision.objects.filter(order=self.order).count(), 1)
        self.order.status = Order.Status.PREVIEW_REVIEW
        self.order.save(update_fields=["status"])
        with self.assertRaises(PreviewFeedbackError):
            PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.CLOTHES)


class MaxClothesRevisionFlowTests(TestCase):
    URL = "/max/webhook/"
    USER = {"user_id": 7001, "first_name": "Ivan"}

    def setUp(self):
        self.order, self.identity = make_preview_review_order(channel="max", external_user_id="7001")
        env = mock.patch.dict("os.environ", {"MAX_WEBHOOK_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, payload):
        return self.client.post(self.URL, data=json.dumps(payload), content_type="application/json")

    def _callback(self, payload):
        return {
            "update_type": "message_callback",
            "callback": {"callback_id": "cb-1", "payload": payload, "user": self.USER},
            "message": {"recipient": {"chat_id": 9001}, "body": {"mid": "mid-1"}},
        }

    def _text(self, text):
        return {
            "update_type": "message_created",
            "message": {"sender": self.USER, "recipient": {"chat_id": 9001}, "body": {"mid": "mid-2", "text": text}},
        }

    def test_button_then_optional_text(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value
            self.assertEqual(self._post(self._callback("preview_revision")).status_code, 200)
            labels = [b[0]["text"] for b in client.send_message.call_args.kwargs["buttons"]]
            payloads = [b[0]["payload"] for b in client.send_message.call_args.kwargs["buttons"]]
            self.assertIn("Сменить одежду", labels)
            self.assertIn("preview_revision:clothes", payloads)

            self.assertEqual(self._post(self._callback("preview_revision:clothes")).status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], REVISION_CLOTHES_ACCEPTED_TEXT)
            revision = Revision.objects.get(order=self.order)
            self.assertEqual(revision.category, Revision.Category.CLOTHES)
            self.assertEqual(revision.customer_text, "")

            self.assertEqual(self._post(self._text("деловой костюм")).status_code, 200)
            revision.refresh_from_db()
            self.assertEqual(revision.customer_text, "деловой костюм")
            self.assertEqual(
                client.send_message.call_args.kwargs["text"],
                REVISION_TEXT_SAVED_TEXT.format(text="деловой костюм"),
            )
            self.assertEqual(Revision.objects.filter(order=self.order).count(), 1)

    def test_other_category_keeps_plain_accepted_text(self):
        with mock.patch("apps.max_bot.views.MaxBotClient") as client_cls:
            client = client_cls.return_value
            self.assertEqual(self._post(self._callback("preview_revision:face")).status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], REVISION_ACCEPTED_TEXT)


@override_settings(TELEGRAM_WEBHOOK_SECRET="", TELEGRAM_BOT_TOKEN="test-token")
class TelegramClothesRevisionFlowTests(TestCase):
    URL = "/telegram/webhook/"
    USER = {"id": 3301, "first_name": "Olga"}
    CHAT_ID = 4301

    def setUp(self):
        self.order, self.identity = make_preview_review_order(channel="telegram", external_user_id="3301")

    def _post(self, payload):
        return self.client.post(self.URL, data=json.dumps(payload), content_type="application/json")

    def _callback(self, data):
        return {"callback_query": {"id": "cb-1", "from": self.USER, "message": {"chat": {"id": self.CHAT_ID}}, "data": data}}

    def _text(self, text):
        return {"message": {"from": self.USER, "chat": {"id": self.CHAT_ID}, "text": text}}

    def test_button_then_optional_text(self):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient") as client_cls:
            client = client_cls.return_value
            self.assertEqual(self._post(self._callback("preview_revision")).status_code, 200)
            keyboard = client.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
            self.assertIn("Сменить одежду", [row[0]["text"] for row in keyboard])
            self.assertIn("preview_revision:clothes", [row[0]["callback_data"] for row in keyboard])

            self.assertEqual(self._post(self._callback("preview_revision:clothes")).status_code, 200)
            self.assertEqual(client.send_message.call_args.kwargs["text"], REVISION_CLOTHES_ACCEPTED_TEXT)
            revision = Revision.objects.get(order=self.order)
            self.assertEqual(revision.category, Revision.Category.CLOTHES)

            self.assertEqual(self._post(self._text("в футболку с котом")).status_code, 200)
            revision.refresh_from_db()
            self.assertEqual(revision.customer_text, "в футболку с котом")
            self.assertEqual(
                client.send_message.call_args.kwargs["text"],
                REVISION_TEXT_SAVED_TEXT.format(text="в футболку с котом"),
            )
            self.assertEqual(Revision.objects.filter(order=self.order).count(), 1)
