import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings

from apps.core.models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, Product, Style, User
from apps.core.services.preview_delivery import DeliveryResult, PreviewDeliveryError, PreviewDeliveryService
from apps.core.storage import LocalMediaStorage


class FakeAdapter:
    channel = ChannelIdentity.Channel.TELEGRAM

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def send_preview(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("channel unavailable")
        return DeliveryResult(message_id="msg-42", metadata={"ok": True})


class PreviewDeliveryServiceTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="telegram", external_user_id="100")
        product = Product.objects.create(code="stickers", name="Sticker Pack")
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style, status=Order.Status.INTERNAL_PREVIEW_REVIEW)
        job = GenerationJob.objects.create(order=self.order, task_type="preview", status="succeeded", attempt=1, provider="fake")
        self.storage = LocalMediaStorage()
        self.storage.save("generated/preview.png", BytesIO(b"preview"))
        self.asset = GeneratedAsset.objects.create(order=self.order, job=job, storage_key="generated/preview.png", size_bytes=7, metadata={"internal_approved": True})

    def test_success_records_delivery_and_transitions(self):
        adapter = FakeAdapter()
        PreviewDeliveryService(adapter=adapter, storage=self.storage).deliver(order=self.order)
        self.order.refresh_from_db()
        self.asset.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)
        self.assertEqual(self.asset.metadata["deliveries"][-1]["message_id"], "msg-42")

    def test_failure_keeps_internal_review(self):
        with self.assertRaises(PreviewDeliveryError):
            PreviewDeliveryService(adapter=FakeAdapter(fail=True), storage=self.storage).deliver(order=self.order)
        self.order.refresh_from_db()
        self.asset.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(self.asset.metadata["deliveries"][-1]["status"], "failed")

    def test_duplicate_success_is_blocked(self):
        adapter = FakeAdapter()
        service = PreviewDeliveryService(adapter=adapter, storage=self.storage)
        service.deliver(order=self.order)
        self.order.status = Order.Status.INTERNAL_PREVIEW_REVIEW
        self.order.save(update_fields=["status", "updated_at"])
        with self.assertRaisesRegex(PreviewDeliveryError, "already delivered"):
            service.deliver(order=self.order)
        self.assertEqual(adapter.calls, 1)
