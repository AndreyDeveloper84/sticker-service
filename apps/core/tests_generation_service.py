import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, OrderPhoto, Product, Style, User
from apps.core.services.generation import GenerationError, GenerationService
from apps.core.storage import LocalMediaStorage


class FakeProvider:
    name = "fake"

    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return ImageGenerationResult(content=b"preview", metadata={"fake": True})


class GenerationServiceTests(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="telegram", external_user_id="gen-user")
        product = Product.objects.create(code="stickers", name="Stickers", config={"generation_prompt": "Make preview"})
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Preserve likeness"})
        self.order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID, customer_notes="Keep glasses")

    def add_photo(self, storage):
        key = f"orders/{self.order.pk}/reference.jpg"
        storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=key, original_filename="reference.jpg", mime_type="image/jpeg", size_bytes=12)

    def test_success_saves_asset_and_enters_internal_review(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage()
            self.add_photo(storage)
            provider = FakeProvider()
            asset = GenerationService(provider=provider, storage=storage).generate_preview(order=self.order)
            self.order.refresh_from_db()
            job = GenerationJob.objects.get(order=self.order)
            self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
            self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
            self.assertEqual(asset.kind, GeneratedAsset.Kind.PREVIEW)
            self.assertTrue(storage.exists(asset.storage_key))
            self.assertEqual(provider.requests[0].reference_images[0].content, b"person-photo")
            self.assertIn("Keep glasses", provider.requests[0].prompt)

    def test_failure_then_retry_preserves_attempt_history(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage()
            self.add_photo(storage)
            with self.assertRaises(GenerationError):
                GenerationService(provider=FakeProvider(True), storage=storage).generate_preview(order=self.order)
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)
            first = GenerationJob.objects.get(order=self.order)
            self.assertEqual(first.status, GenerationJob.Status.FAILED)

            asset = GenerationService(provider=FakeProvider(), storage=storage).generate_preview(order=self.order)
            jobs = list(GenerationJob.objects.filter(order=self.order).order_by("attempt"))
            self.assertEqual([job.attempt for job in jobs], [1, 2])
            self.assertEqual(jobs[0].status, GenerationJob.Status.FAILED)
            self.assertEqual(jobs[1].status, GenerationJob.Status.SUCCEEDED)
            self.assertEqual(asset.job_id, jobs[1].pk)

    def test_unpaid_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage()
            self.add_photo(storage)
            self.order.status = Order.Status.AWAITING_PAYMENT
            self.order.save(update_fields=["status"])
            with self.assertRaises(GenerationError):
                GenerationService(provider=FakeProvider(), storage=storage).generate_preview(order=self.order)
            self.assertFalse(GenerationJob.objects.filter(order=self.order).exists())
