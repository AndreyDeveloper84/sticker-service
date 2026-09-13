import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, Order, OrderPhoto, Product, Style, User
from apps.core.services.generation import GenerationError, GenerationService
from apps.core.services.preview_delivery import DeliveryResult, PreviewDeliveryError, PreviewDeliveryService
from apps.core.storage import LocalMediaStorage


class FakeImageProvider:
    name = "fake-image"

    def generate_preview(self, request):
        return ImageGenerationResult(content=b"preview", mime_type="image/png", metadata={})


class FakeDelivery:
    def __init__(self, channel, *, fail=False):
        self.channel = channel
        self.fail = fail

    def send_preview(self, **kwargs):
        if self.fail:
            raise RuntimeError(f"{self.channel} unavailable")
        return DeliveryResult(message_id=f"{self.channel}-1", metadata={})


class M2FailureIsolationSmokeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.storage = LocalMediaStorage()
        self.product = Product.objects.create(code="stickers-fail", name="Sticker Pack", config={"generation_prompt": "Create preview"})
        self.style = Style.objects.create(code="comic-fail", name="Comic", config={"prompt": "Preserve likeness"})

    def make_order(self, channel, external_user_id, status):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel=channel, external_user_id=external_user_id)
        order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=self.product,
            style=self.style,
            status=status,
        )
        key = f"orders/{order.pk}/source.jpg"
        self.storage.save(key, BytesIO(b"source"))
        OrderPhoto.objects.create(order=order, storage_key=key, original_filename="source.jpg", mime_type="image/jpeg", size_bytes=6)
        return order

    @staticmethod
    def approve_asset(asset):
        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])

    def test_unpaid_generation_rejected_and_channel_failure_does_not_break_other_channel(self):
        unpaid = self.make_order(ChannelIdentity.Channel.TELEGRAM, "tg-unpaid", Order.Status.AWAITING_PAYMENT)
        with self.assertRaises(GenerationError):
            GenerationService(provider=FakeImageProvider(), storage=self.storage).generate_preview(order=unpaid)

        telegram = self.make_order(ChannelIdentity.Channel.TELEGRAM, "tg-fail", Order.Status.PAID)
        max_order = self.make_order(ChannelIdentity.Channel.MAX, "max-ok", Order.Status.PAID)
        for order in (telegram, max_order):
            asset = GenerationService(provider=FakeImageProvider(), storage=self.storage).generate_preview(order=order)
            self.approve_asset(asset)
            order.refresh_from_db()

        with self.assertRaises(PreviewDeliveryError):
            PreviewDeliveryService(
                adapter=FakeDelivery(ChannelIdentity.Channel.TELEGRAM, fail=True),
                storage=self.storage,
            ).deliver(order=telegram)
        telegram.refresh_from_db()
        self.assertEqual(telegram.status, Order.Status.INTERNAL_PREVIEW_REVIEW)

        PreviewDeliveryService(
            adapter=FakeDelivery(ChannelIdentity.Channel.MAX),
            storage=self.storage,
        ).deliver(order=max_order)
        max_order.refresh_from_db()
        self.assertEqual(max_order.status, Order.Status.PREVIEW_REVIEW)
