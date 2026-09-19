import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, Order, OrderPhoto, Product, Revision, Style, User
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION
from apps.core.services.generation import GenerationService
from apps.core.services.preview_delivery import DeliveryResult, PreviewDeliveryService
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.core.storage import LocalMediaStorage
from apps.max_bot.payments import CheckoutSession, MaxExternalPaymentAdapter, PaymentConfirmation


class FakeImageProvider:
    name = "fake-image"

    def __init__(self):
        self.calls = 0

    def generate_preview(self, request):
        self.calls += 1
        return ImageGenerationResult(content=f"preview-{self.calls}".encode(), mime_type="image/png", metadata={})


class FakeMaxDelivery:
    channel = ChannelIdentity.Channel.MAX

    def send_preview(self, **kwargs):
        return DeliveryResult(message_id="max-1", metadata={"recipient_id": kwargs["recipient_id"]})


class FakeMaxPaymentProvider:
    name = "external"

    def __init__(self):
        self.payment = None

    def create_checkout(self, *, payment):
        self.payment = payment
        return CheckoutSession(checkout_url="https://pay.example.test/session", provider_reference="ref")

    def parse_webhook(self, *, body, signature):
        return PaymentConfirmation(
            payment_id=self.payment.pk,
            external_payment_id="max-payment-1",
            status="succeeded",
            metadata={"signature": signature},
        )


class MaxM2SmokeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.storage = LocalMediaStorage()

        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-smoke")
        product = Product.objects.create(
            code="stickers-max",
            name="Sticker Pack",
            config={"price_minor": 9900, "currency": "RUB", "generation_prompt": "Create preview"},
        )
        style = Style.objects.create(code="comic-max", name="Comic", config={"prompt": "Preserve likeness"})
        self.order = Order.objects.create(
            user=user,
            channel_identity=self.identity,
            product=product,
            style=style,
            status=Order.Status.READY_FOR_CHECKOUT,
            consent_version=PILOT_CONSENT_VERSION,
            consent_accepted_at=timezone.now(),
        )
        key = f"orders/{self.order.pk}/source.jpg"
        self.storage.save(key, BytesIO(b"source"))
        OrderPhoto.objects.create(order=self.order, storage_key=key, original_filename="source.jpg", mime_type="image/jpeg", size_bytes=6)

    @staticmethod
    def approve_asset(asset):
        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])

    def test_external_payment_to_one_revision(self):
        provider = FakeMaxPaymentProvider()
        payments = MaxExternalPaymentAdapter(provider=provider)
        payment, session = payments.create_checkout(identity=self.identity)
        self.assertTrue(session.checkout_url.startswith("https://"))
        first_confirmation = payments.confirm_webhook(body=b"{}", signature="sig")
        second_confirmation = payments.confirm_webhook(body=b"{}", signature="sig")
        self.assertEqual(payment.pk, first_confirmation.pk)
        self.assertEqual(first_confirmation.pk, second_confirmation.pk)

        self.order.refresh_from_db()
        generation = GenerationService(provider=FakeImageProvider(), storage=self.storage)
        first = generation.generate_preview(order=self.order)
        self.order.refresh_from_db()
        self.approve_asset(first)
        PreviewDeliveryService(adapter=FakeMaxDelivery(), storage=self.storage).deliver(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)

        revision = PreviewFeedbackService.request_revision(
            order=self.order,
            category=Revision.Category.FACE,
            customer_text="Сделать лицо ближе к фотографии",
        )
        repeated = PreviewFeedbackService.request_revision(
            order=self.order,
            category=Revision.Category.FACE,
            customer_text="Сделать лицо ближе к фотографии",
        )
        self.assertEqual(revision.pk, repeated.pk)

        self.order.refresh_from_db()
        revised = generation.generate_revision(order=self.order)
        revision.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(revision.status, Revision.Status.COMPLETED)
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(revised.job.input_metadata["source_preview_id"], first.pk)

        # The rejected preview lost its internal approval at request_revision:
        # the revised one is approved and sent without any manual clean-up.
        first.refresh_from_db()
        self.assertNotIn("internal_approved", first.metadata)
        self.approve_asset(revised)
        PreviewDeliveryService(adapter=FakeMaxDelivery(), storage=self.storage).deliver(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)

        with self.assertRaises(PreviewFeedbackError):
            PreviewFeedbackService.request_revision(
                order=self.order,
                category=Revision.Category.HAIR,
                customer_text="Вторая бесплатная правка",
            )
        self.assertEqual(Revision.objects.filter(order=self.order).count(), 1)
