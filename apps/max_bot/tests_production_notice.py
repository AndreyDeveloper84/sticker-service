"""MAX "in production" notice after the operator starts full production.

Best-effort and at most once per order: the first start() that moves the
order into PACK_GENERATING sends exactly one message; the per-slot re-entries
of start() (max_slots batching) do not send again; non-MAX orders get
nothing; a MAX failure neither breaks the console view nor touches the
production state.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderPhoto,
    Payment,
    Product,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.full_production import FullProductionService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_qc import make_image
from apps.max_bot.client import MaxAPIError
from apps.max_bot.production_notice import (
    PRODUCTION_NOTICE_KEY,
    PRODUCTION_NOTICE_TEXT,
    notify_customer_production_started,
)

EMOTIONS = [{"code": "e0", "label": "E0"}, {"code": "e1", "label": "E1"}, {"code": "e2", "label": "E2"}]
PACK3 = {"kind": "pack", "quantity": 3, "emotion_count": 3, "emotions": EMOTIONS, "price_minor": 50000}


class StickerProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


class FakeMaxClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent = []

    def send_message(self, *, chat_id=None, user_id=None, text, buttons=None, attachments=None):
        if self.fail:
            raise MaxAPIError(502, "max down")
        self.sent.append({"chat_id": chat_id, "user_id": user_id, "text": text})
        return {"message": {"mid": f"mid-{len(self.sent)}"}}


class ProductionNoticeFixture(TestCase):
    channel = ChannelIdentity.Channel.MAX

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()

        self.operator = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password="pass"
        )
        self.client.force_login(self.operator)

        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=self.channel, external_user_id="max-prod-1"
        )
        product = Product.objects.create(code="pack3", name="Pack 3", config=PACK3)
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=self.identity,
            product=product,
            style=style,
            status=Order.Status.PREVIEW_REVIEW,
            selection={"emotions": [e["code"] for e in EMOTIONS]},
        )
        photo_key = f"orders/{self.order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(
            order=self.order, storage_key=photo_key, original_filename="photo.jpg",
            mime_type="image/jpeg", size_bytes=5,
        )
        self.payment = Payment.objects.create(
            order=self.order, provider="yookassa", status=Payment.Status.CONFIRMED,
            amount_minor=50000, currency="RUB", external_payment_id="yk-1",
            confirmed_at=timezone.now(), metadata={"channel": "max"},
        )
        preview_job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED, attempt=1, provider="fake",
        )
        preview_key = f"generated/order-{self.order.pk}/preview/job-{preview_job.pk}.png"
        self.storage.save(preview_key, BytesIO(b"preview"))
        GeneratedAsset.objects.create(
            order=self.order, job=preview_job, kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=preview_key, size_bytes=7,
            metadata={"internal_approved": True, "customer_approved": True},
        )

        storage = self.storage
        patcher = patch.object(
            ProductionOrderAdmin,
            "get_full_production_service",
            lambda self_: FullProductionService(provider=StickerProvider(), storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.max_client = FakeMaxClient()

    def _start(self, *, max_client=None):
        url = reverse("admin:core_order_start_full_production", args=[self.order.pk])
        with patch("apps.core.production_console.MaxBotClient", return_value=max_client or self.max_client):
            response = self.client.post(url, data={}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][1], 302)
        return [str(m) for m in response.context["messages"]]

    def _notice(self):
        self.payment.refresh_from_db()
        return (self.payment.metadata or {}).get(PRODUCTION_NOTICE_KEY) or {}


class ProductionNoticeConsoleTests(ProductionNoticeFixture):
    def test_first_start_sends_exactly_one_notice(self):
        messages = self._start()

        self.assertTrue(messages[0].startswith("Full production plan:"), messages)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(len(self.max_client.sent), 1)
        self.assertEqual(self.max_client.sent[0]["user_id"], "max-prod-1")
        self.assertIsNone(self.max_client.sent[0]["chat_id"])
        self.assertEqual(self.max_client.sent[0]["text"], PRODUCTION_NOTICE_TEXT)
        notice = self._notice()
        self.assertEqual(notice["status"], "sent")
        self.assertEqual(notice["message_id"], "mid-1")

    def test_slot_batching_re_entries_do_not_send_again(self):
        for _ in range(3):  # one slot per run → PACK_GENERATING ×2, then QUALITY_CONTROL
            self._start()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        self.assertEqual(len(self.max_client.sent), 1)

    def test_max_failure_keeps_view_and_production_state(self):
        failing = FakeMaxClient(fail=True)

        with self.assertLogs("apps.max_bot.production_notice", level="WARNING"):
            messages = self._start(max_client=failing)

        self.assertTrue(messages[0].startswith("Full production plan:"), messages)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.FULL).count(), 1)
        notice = self._notice()
        self.assertEqual(notice["status"], "failed")
        self.assertEqual(notice["error"], "MaxAPIError")

        # at most once: the next slot run does not retry the failed notice
        self._start()
        self.assertEqual(len(self.max_client.sent), 0)

    def test_failed_start_sends_nothing(self):
        self.order.status = Order.Status.PAID  # cannot enter production from here
        self.order.save(update_fields=["status"])

        messages = self._start()

        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0].startswith("Full production plan:"), messages)
        self.assertEqual(self.max_client.sent, [])
        self.assertEqual(self._notice(), {})


class ProductionNoticeTelegramTests(ProductionNoticeFixture):
    channel = ChannelIdentity.Channel.TELEGRAM

    def test_telegram_order_gets_no_max_notice(self):
        messages = self._start()

        self.assertTrue(messages[0].startswith("Full production plan:"), messages)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(self.max_client.sent, [])
        self.assertEqual(self._notice(), {})


class ProductionNoticeUnitTests(ProductionNoticeFixture):
    def test_not_in_production_sends_nothing(self):
        client = FakeMaxClient()
        self.assertFalse(notify_customer_production_started(order=self.order, client=client))
        self.assertEqual(client.sent, [])
        self.assertEqual(self._notice(), {})

    def test_without_confirmed_payment_sends_nothing(self):
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        self.payment.status = Payment.Status.PENDING
        self.payment.save(update_fields=["status"])
        client = FakeMaxClient()
        self.assertFalse(notify_customer_production_started(order=self.order, client=client))
        self.assertEqual(client.sent, [])

    def test_sends_once_and_records_on_latest_confirmed_payment(self):
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        client = FakeMaxClient()
        self.assertTrue(notify_customer_production_started(order=self.order, client=client))
        self.assertFalse(notify_customer_production_started(order=self.order, client=client))
        self.assertEqual(len(client.sent), 1)
        notice = self._notice()
        self.assertEqual(notice["status"], "sent")
        self.assertTrue(notice["claimed_at"])
        self.assertTrue(notice["finished_at"])
        # paid_notice state on the same payment is untouched
        self.assertEqual(self.payment.metadata.get("channel"), "max")

    def test_unexpected_error_is_swallowed(self):
        with patch("apps.max_bot.production_notice._claim", side_effect=RuntimeError("boom")), self.assertLogs(
            "apps.max_bot.production_notice", level="ERROR"
        ):
            self.assertFalse(notify_customer_production_started(order=self.order, client=FakeMaxClient()))
