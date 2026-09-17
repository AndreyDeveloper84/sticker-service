"""Regression: production actions through the REGISTERED Order admin.

The registered admin is the top of the console inheritance chain
(Production → PreviewDelivery → Qc → FinalDelivery). Subclasses must not
shadow base helpers with incompatible signatures: after DRF-2053,
``FinalDeliveryOrderAdmin._plan_message(DeliveryPlan)`` overrode
``ProductionOrderAdmin._plan_message(list[SlotState])`` and every full
production action raised ``AttributeError: 'list' object has no attribute
'slots'`` (500 on staging). These tests drive the actions through
``admin.site`` exactly as the operator does.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.final_delivery_console import FinalDeliveryOrderAdmin
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
from apps.core.services.final_delivery import FinalDeliveryService
from apps.core.services.full_production import FullProductionService
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_final_delivery import FakeAdapter
from apps.core.tests_qc import make_image

EMOTIONS = [{"code": "e0", "label": "E0"}, {"code": "e1", "label": "E1"}, {"code": "e2", "label": "E2"}]
PACK3 = {"kind": "pack", "quantity": 3, "emotion_count": 3, "emotions": EMOTIONS, "price_minor": 50000}


class StickerProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


class RegisteredAdminProductionActionsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()
        self.provider = StickerProvider()

        self.operator = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password="pass"
        )
        self.client.force_login(self.operator)

        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="100"
        )
        product = Product.objects.create(code="pack3", name="Pack 3", config=PACK3)
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
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
        Payment.objects.create(
            order=self.order, provider="telegram_stars", status=Payment.Status.CONFIRMED,
            amount_minor=460, currency="XTR", external_payment_id="charge-1",
            confirmed_at=timezone.now(),
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

        provider, storage = self.provider, self.storage
        patcher = patch.object(
            ProductionOrderAdmin,
            "get_full_production_service",
            lambda self_: FullProductionService(provider=provider, storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, url, data=None):
        """POST and follow the redirect: the rendered change page consumes
        exactly the messages this action produced (never a 500)."""
        response = self.client.post(url, data=data or {}, follow=True)
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(response.redirect_chain[-1][1], 302, url)
        return [str(m) for m in response.context["messages"]]

    def test_registered_admin_is_the_delivery_console(self):
        self.assertIsInstance(admin.site._registry[Order], FinalDeliveryOrderAdmin)

    def test_start_full_production_through_registered_admin(self):
        url = reverse("admin:core_order_start_full_production", args=[self.order.pk])
        for step in range(1, 4):
            messages = self._post(url)
            self.assertEqual(len(messages), 1, f"slot {step}")
            self.assertTrue(messages[0].startswith("Full production plan:"), messages[0])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        self.assertIn("e0: succeeded, e1: succeeded, e2: succeeded", messages[0])

    def test_retry_and_regenerate_through_registered_admin_do_not_crash(self):
        self._post(reverse("admin:core_order_start_full_production", args=[self.order.pk]))
        for name in ("core_order_retry_failed_production", "core_order_regenerate_slots"):
            data = {"slot_keys": "e0"} if name == "core_order_regenerate_slots" else {}
            # either a plan message or a domain error message — never a 500
            messages = self._post(reverse(f"admin:{name}", args=[self.order.pk]), data=data)
            self.assertEqual(len(messages), 1, name)

    def test_deliver_final_still_reports_delivery_plan(self):
        start = reverse("admin:core_order_start_full_production", args=[self.order.pk])
        for _ in range(3):
            self._post(start)
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=self.order)
        qc.finalize_report(report=report, checklist={c: True for c in HUMAN_CRITERIA})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.READY_FOR_DELIVERY)

        adapter = FakeAdapter()
        service = FinalDeliveryService(adapter=adapter, storage=self.storage)
        with patch.object(FinalDeliveryOrderAdmin, "get_final_delivery_service", return_value=service):
            messages = self._post(reverse("admin:core_order_deliver_final", args=[self.order.pk]))
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("Final set delivered:"), messages[0])
        self.assertEqual(len(adapter.items), 3)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
