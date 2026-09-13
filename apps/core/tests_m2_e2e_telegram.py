import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, Order, OrderPhoto, Product, Style, User
from apps.core.services.generation import GenerationService
from apps.core.services.preview_delivery import DeliveryResult, PreviewDeliveryService
from apps.core.services.preview_feedback import PreviewFeedbackService
from apps.core.storage import LocalMediaStorage
from apps.telegram_bot.payments import TelegramStarsPaymentAdapter


class FakeImageProvider:
    name = "fake-image"

    def generate_preview(self, request):
        return ImageGenerationResult(content=b"preview", mime_type="image/png", metadata={})


class FakeTelegramDelivery:
    channel = ChannelIdentity.Channel.TELEGRAM

    def send_preview(self, **kwargs):
        return DeliveryResult(message_id="tg-1", metadata={"recipient_id": kwargs["recipient_id"]})


class TelegramM2SmokeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.storage = LocalMediaStorage()

        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-smoke",
        )
        product = Product.objects.create(
            code="stickers",
            name="Sticker Pack",
            config={"price_stars": 150, "generation_prompt": "Create preview"},
        )
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Preserve likeness"})
        self.order = Order.objects.create(
            user=user,
            channel_identity=self.identity,
            product=product,
            style=style,
            status=Order.Status.READY_FOR_CHECKOUT,
        )
        key = f"orders/{self.order.pk}/source.jpg"
        self.storage.save(key, BytesIO(b"source"))
        OrderPhoto.objects.create(
            order=self.order,
            storage_key=key,
            original_filename="source.jpg",
            mime_type="image/jpeg",
            size_bytes=6,
        )

    def test_stars_payment_to_preview_delivery_and_approve(self):
        payments = TelegramStarsPaymentAdapter()
        payment = payments.payment_for_identity(self.identity)
        successful = {
            "invoice_payload": payments.payload(payment),
            "currency": "XTR",
            "total_amount": payment.amount_minor,
            "telegram_payment_charge_id": "tg-charge-1",
            "provider_payment_charge_id": "provider-charge-1",
        }
        first_confirmation = payments.confirm_successful_payment(identity=self.identity, successful_payment=successful)
        second_confirmation = payments.confirm_successful_payment(identity=self.identity, successful_payment=successful)
        self.assertEqual(first_confirmation.pk, second_confirmation.pk)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        asset = GenerationService(provider=FakeImageProvider(), storage=self.storage).generate_preview(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(asset.order_id, self.order.pk)
        self.assertEqual(asset.job.order_id, self.order.pk)

        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])

        PreviewDeliveryService(adapter=FakeTelegramDelivery(), storage=self.storage).deliver(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)

        approved = PreviewFeedbackService.approve(order=self.order)
        duplicate = PreviewFeedbackService.approve(order=self.order)
        self.assertEqual(approved.pk, asset.pk)
        self.assertEqual(duplicate.pk, asset.pk)
        asset.refresh_from_db()
        self.assertTrue(asset.metadata.get("customer_approved"))
