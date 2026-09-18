"""DRF-2066: Production Console action "Generate / Retry Revision".

The action is a thin console entry over GenerationService.generate_revision
(the only revision implementation): the service owns REVISION_REQUESTED ->
REVISION_GENERATING -> INTERNAL_PREVIEW_REVIEW, the one-revision limit and
job/asset bookkeeping. These tests cover the console surface only.
"""

import tempfile
from io import BytesIO
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
    Revision,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.generation import GenerationService
from apps.core.services.order_state import OrderStateService
from apps.core.services.preview_feedback import PreviewFeedbackService
from apps.core.storage import LocalMediaStorage


class FakeProvider:
    name = "fake"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return ImageGenerationResult(
            content=f"image-{len(self.requests)}".encode(),
            mime_type="image/png",
            metadata={"fake": True},
        )


class ProductionConsoleRevisionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()

        self.admin = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password="pass"
        )
        self.client.force_login(self.admin)

        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="100"
        )
        product = Product.objects.create(
            code="stickers", name="Sticker Pack", config={"generation_prompt": "Generate preview"}
        )
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Comic style"})
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
            status=Order.Status.PAID,
        )
        self.storage.save("orders/source.jpg", BytesIO(b"source"))
        OrderPhoto.objects.create(
            order=self.order,
            storage_key="orders/source.jpg",
            original_filename="source.jpg",
            mime_type="image/jpeg",
            size_bytes=6,
        )
        self.provider = FakeProvider()
        self.change_url = reverse("admin:core_order_change", args=[self.order.pk])
        self.action_url = reverse("admin:core_order_generate_revision", args=[self.order.pk])

    # ---------------------------------------------------------- fixture

    def service(self, provider=None):
        return GenerationService(provider=provider or self.provider, storage=self.storage)

    def _patched(self, provider=None):
        return patch.object(
            ProductionOrderAdmin, "get_generation_service", return_value=self.service(provider)
        )

    def request_revision(self, *, category=Revision.Category.FACE, text="Make the face closer"):
        """Real customer path: preview -> internal approve -> delivered -> revision."""
        source = self.service().generate_preview(order=self.order)
        self.order.refresh_from_db()
        metadata = dict(source.metadata or {})
        metadata.update(
            {
                "internal_approved": True,
                "deliveries": [{"status": "sent", "channel": "telegram", "message_id": "1"}],
            }
        )
        source.metadata = metadata
        source.save(update_fields=["metadata", "updated_at"])
        OrderStateService.transition(order=self.order, to_status=Order.Status.PREVIEW_REVIEW)
        revision = PreviewFeedbackService.request_revision(
            order=self.order, category=category, customer_text=text
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REVISION_REQUESTED)
        return source, revision

    # ------------------------------------------------------------ tests

    def test_happy_path_generates_revision_and_returns_to_internal_review(self):
        source, revision = self.request_revision()

        with self._patched():
            response = self.client.post(self.action_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)

        jobs = GenerationJob.objects.filter(order=self.order).order_by("created_at")
        self.assertEqual(
            [(job.task_type, job.status) for job in jobs],
            [
                (GenerationJob.TaskType.PREVIEW, GenerationJob.Status.SUCCEEDED),
                (GenerationJob.TaskType.REVISION, GenerationJob.Status.SUCCEEDED),
            ],
        )
        revision_job = jobs.last()
        self.assertEqual(revision_job.input_metadata["revision_id"], revision.pk)
        self.assertEqual(revision_job.input_metadata["source_preview_id"], source.pk)

        assets = GeneratedAsset.objects.filter(
            order=self.order, kind=GeneratedAsset.Kind.PREVIEW
        ).order_by("created_at")
        self.assertEqual(assets.count(), 2)
        self.assertEqual(assets.first().pk, source.pk)  # old preview preserved
        new_asset = assets.last()
        self.assertEqual(new_asset.job_id, revision_job.pk)
        self.assertTrue(self.storage.exists(new_asset.storage_key))

        revision.refresh_from_db()
        self.assertEqual(revision.status, Revision.Status.COMPLETED)
        self.assertIn("Make the face closer", self.provider.requests[-1].prompt)

        self.assertContains(response, f"Превью с правкой #{new_asset.pk} сгенерировано")
        self.assertContains(response, f"генерация #{revision_job.pk}")
        # Back on internal review: the usual preview actions, no revision button.
        self.assertContains(response, "Перегенерировать превью")
        self.assertNotContains(response, "Сгенерировать правку")

    def test_get_shows_confirmation_and_does_not_generate(self):
        self.request_revision()
        with self._patched():
            response = self.client.get(self.action_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сгенерировать правку")
        self.assertContains(response, f'action="{self.action_url}"')
        # Only the fixture preview call reached the provider, no revision call.
        self.assertEqual(
            [r.metadata["task_type"] for r in self.provider.requests],
            [GenerationJob.TaskType.PREVIEW],
        )
        self.assertFalse(
            GenerationJob.objects.filter(
                order=self.order, task_type=GenerationJob.TaskType.REVISION
            ).exists()
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REVISION_REQUESTED)

    def test_action_rejected_outside_revision_statuses_without_state_change(self):
        self.request_revision()
        for status in (
            Order.Status.PAID,
            Order.Status.INTERNAL_PREVIEW_REVIEW,
            Order.Status.PREVIEW_REVIEW,
            Order.Status.PACK_GENERATING,
            Order.Status.QUALITY_CONTROL,
        ):
            with self.subTest(status=status):
                Order.objects.filter(pk=self.order.pk).update(status=status)
                with self._patched():
                    response = self.client.post(self.action_url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, "только из статусов «Правка запрошена» / «Генерация правки»"
                )
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, status)
                self.assertFalse(
                    GenerationJob.objects.filter(
                        order=self.order, task_type=GenerationJob.TaskType.REVISION
                    ).exists()
                )
        self.assertEqual(len(self.provider.requests), 1)  # only the fixture preview

    def test_service_error_is_reported_without_masking_state(self):
        # REVISION_REQUESTED but no Revision row: the service refuses.
        self.service().generate_preview(order=self.order)
        Order.objects.filter(pk=self.order.pk).update(status=Order.Status.REVISION_REQUESTED)
        with self._patched():
            response = self.client.post(self.action_url, follow=True)
        self.assertContains(response, "Клиент не запрашивал правку.")
        self.order.refresh_from_db()
        # _start_job is atomic: the REVISION_GENERATING transition rolled back.
        self.assertEqual(self.order.status, Order.Status.REVISION_REQUESTED)
        self.assertFalse(
            GenerationJob.objects.filter(
                order=self.order, task_type=GenerationJob.TaskType.REVISION
            ).exists()
        )

    def test_failed_provider_shows_failed_job_and_allows_retry(self):
        source, revision = self.request_revision()
        failing = FakeProvider(fail=True)

        with self._patched(failing):
            response = self.client.post(self.action_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "provider unavailable")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REVISION_GENERATING)
        failed = GenerationJob.objects.get(order=self.order, task_type=GenerationJob.TaskType.REVISION)
        self.assertEqual(failed.status, GenerationJob.Status.FAILED)
        self.assertIn("provider unavailable", failed.error)
        # Generation history on the order page exposes the failure ...
        self.assertContains(response, "попытка 1 · правка · ошибка · fake · provider unavailable")
        # ... no new asset, the source preview is untouched, revision still open.
        self.assertEqual(
            GeneratedAsset.objects.filter(order=self.order, kind=GeneratedAsset.Kind.PREVIEW).count(),
            1,
        )
        revision.refresh_from_db()
        self.assertEqual(revision.status, Revision.Status.GENERATING)
        # Retry is offered from REVISION_GENERATING (service accepts re-entry).
        self.assertContains(response, "Сгенерировать правку")
        self.assertContains(response, self.action_url)

        with self._patched():
            response = self.client.post(self.action_url, follow=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        attempts = list(
            GenerationJob.objects.filter(order=self.order, task_type=GenerationJob.TaskType.REVISION)
            .order_by("attempt")
            .values_list("attempt", "status")
        )
        self.assertEqual(
            attempts,
            [(1, GenerationJob.Status.FAILED), (2, GenerationJob.Status.SUCCEEDED)],
        )
        revision.refresh_from_db()
        self.assertEqual(revision.status, Revision.Status.COMPLETED)
        self.assertContains(response, "Превью с правкой #")

    def test_button_rendered_only_in_revision_statuses(self):
        self.request_revision()
        shown = {Order.Status.REVISION_REQUESTED, Order.Status.REVISION_GENERATING}
        for status in Order.Status:
            with self.subTest(status=status):
                Order.objects.filter(pk=self.order.pk).update(status=status)
                with self._patched():
                    response = self.client.get(self.change_url)
                self.assertEqual(response.status_code, 200)
                if status in shown:
                    self.assertContains(response, "Сгенерировать правку")
                    self.assertContains(response, self.action_url)
                else:
                    self.assertNotContains(response, self.action_url)

    def test_order_page_shows_revision_request_details(self):
        source, revision = self.request_revision(
            category=Revision.Category.HAIR, text="Hair should be curly"
        )
        with self._patched():
            response = self.client.get(self.change_url)
        self.assertContains(response, "Правка клиента")
        self.assertContains(response, f"Правка #{revision.pk}")
        self.assertContains(response, "что исправить: <strong>Волосы</strong>")
        self.assertContains(response, "Hair should be curly")
        self.assertContains(response, f"исходное превью: <a href=\"{reverse('admin:core_preview_asset_file', args=[source.pk])}\"")
        self.assertContains(response, f"#{source.pk}</a>")

    def test_order_page_without_revision_says_so(self):
        with self._patched():
            response = self.client.get(self.change_url)
        self.assertContains(response, "Клиент не запрашивал правку.")
        self.assertNotContains(response, "Сгенерировать правку")
