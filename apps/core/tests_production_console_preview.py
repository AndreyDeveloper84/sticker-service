import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderPhoto,
    Product,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.generation import GenerationService
from apps.core.services.order_state import OrderStateService
from apps.core.storage import LocalMediaStorage


class FakeProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(
            content=b"preview-bytes",
            mime_type="image/png",
            metadata={"fake": True},
        )


class ProductionConsolePreviewTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media_root = Path(self.tmp.name)
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        auth_user = get_user_model()
        self.admin = auth_user.objects.create_superuser(
            username="operator",
            email="operator@example.com",
            password="pass",
        )
        self.client.force_login(self.admin)

        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="100",
        )
        product = Product.objects.create(
            code="stickers",
            name="Sticker Pack",
            config={"generation_prompt": "Generate preview"},
        )
        style = Style.objects.create(
            code="comic",
            name="Comic",
            config={"prompt": "Comic style"},
        )
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
            status=Order.Status.PAID,
        )
        storage = LocalMediaStorage()
        storage.save("orders/source.jpg", __import__("io").BytesIO(b"source"))
        OrderPhoto.objects.create(
            order=self.order,
            storage_key="orders/source.jpg",
            original_filename="source.jpg",
            mime_type="image/jpeg",
            size_bytes=6,
        )

    def service(self):
        return GenerationService(
            provider=FakeProvider(),
            storage=LocalMediaStorage(),
        )

    def test_operator_can_generate_preview_from_order_page(self):
        url = reverse("admin:core_order_generate_preview", args=[self.order.pk])
        with patch.object(
            ProductionOrderAdmin,
            "get_generation_service",
            return_value=self.service(),
        ):
            response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 1)
        self.assertEqual(GeneratedAsset.objects.filter(order=self.order).count(), 1)

    def test_regenerate_preserves_previous_asset_and_creates_new_attempt(self):
        first = self.service().generate_preview(order=self.order)
        self.order.refresh_from_db()

        url = reverse("admin:core_order_regenerate_preview", args=[self.order.pk])
        with patch.object(
            ProductionOrderAdmin,
            "get_generation_service",
            return_value=self.service(),
        ):
            response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        assets = GeneratedAsset.objects.filter(order=self.order).order_by("created_at")
        self.assertEqual(assets.count(), 2)
        self.assertEqual(assets.first().pk, first.pk)
        self.assertEqual(
            list(
                GenerationJob.objects.filter(order=self.order)
                .order_by("attempt")
                .values_list("attempt", flat=True)
            ),
            [1, 2],
        )

    def test_approve_marks_exact_asset_and_waits_for_delivery(self):
        first = self.service().generate_preview(order=self.order)
        self.order.refresh_from_db()
        OrderStateService.transition(
            order=self.order,
            to_status=Order.Status.PREVIEW_GENERATING,
        )
        second = self.service().generate_preview(order=self.order)
        self.order.refresh_from_db()

        url = reverse(
            "admin:core_order_approve_preview",
            args=[self.order.pk, first.pk],
        )
        response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertTrue(first.metadata.get("internal_approved"))
        self.assertFalse(second.metadata.get("internal_approved", False))

    def test_failed_job_is_visible_on_order_change_page(self):
        GenerationJob.objects.create(
            order=self.order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.FAILED,
            attempt=1,
            provider="fake",
            error="provider exploded",
        )
        response = self.client.get(
            reverse("admin:core_order_change", args=[self.order.pk])
        )
        self.assertContains(response, "provider exploded")
        self.assertContains(response, "Failed")
