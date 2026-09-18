"""DRF-2079: QC retry → regeneration path in the Production Console.

Live evidence (staging order 10): after a human-checklist FAIL the operator
pressed "QC retry (slot)" and then "Retry Failed Slots"; the succeeded slot
was preserved, the order flipped back to QUALITY_CONTROL with the same
asset and three QcReports piled up without a single new FULL job. The
console now (1) prefills / offers Regenerate for the pending retry slots,
(2) routes Start / Retry Failed to regenerate_slots while retry slots are
pending, (3) refuses start_qc on an unchanged set, and (4) renders panel
separators as markup instead of literal ``<br>`` / ``&nbsp;``.
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
    QcReport,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.full_production import FullProductionService
from apps.core.services.qc import HUMAN_CRITERIA, QcError, QcService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_qc import make_image

EMOTIONS = [{"code": "hello", "label": "Привет"}, {"code": "bye", "label": "Пока"}]
PACK2 = {"kind": "pack", "quantity": 2, "emotion_count": 2, "emotions": EMOTIONS, "price_minor": 50000}


class StickerProvider:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request.metadata.get("slot_key"))
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


class QcRetryConsoleTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()
        self.provider = StickerProvider()
        self.qc = QcService(storage=self.storage)

        self.client.force_login(
            get_user_model().objects.create_superuser(username="op", email="op@example.com", password="pass")
        )
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="10"
        )
        product = Product.objects.create(code="pack2", name="Pack 2", config=PACK2)
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user, channel_identity=identity, product=product, style=style,
            status=Order.Status.PREVIEW_REVIEW, selection={"emotions": ["hello", "bye"]},
        )
        photo_key = f"orders/{self.order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=photo_key, original_filename="p.jpg", mime_type="image/jpeg", size_bytes=5)
        Payment.objects.create(
            order=self.order, provider="yookassa", status=Payment.Status.CONFIRMED,
            amount_minor=50000, currency="RUB", external_payment_id="yk-1", confirmed_at=timezone.now(),
        )
        job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED, attempt=1, provider="fake",
        )
        key = f"generated/order-{self.order.pk}/preview/{job.pk}.png"
        self.storage.save(key, BytesIO(b"preview"))
        GeneratedAsset.objects.create(
            order=self.order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key=key, size_bytes=7,
            metadata={"internal_approved": True, "customer_approved": True},
        )
        provider, storage = self.provider, self.storage
        patcher = patch.object(
            ProductionOrderAdmin, "get_full_production_service",
            lambda self_: FullProductionService(provider=provider, storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # MAX production notice is best-effort and out of scope here.
        notice = patch.object(ProductionOrderAdmin, "notify_production_started", return_value=False)
        notice.start()
        self.addCleanup(notice.stop)

    # -- helpers --------------------------------------------------------------

    def url(self, name, *args):
        return reverse(f"admin:{name}", args=[self.order.pk, *args])

    def post(self, name, *args, data=None):
        response = self.client.post(self.url(name, *args), data=data or {}, follow=True)
        self.assertEqual(response.status_code, 200)
        return [str(m) for m in response.context["messages"]]

    def produce_and_fail_qc_on(self, slot):
        """FULL → QUALITY_CONTROL → QC FAIL (human) → QC retry(slot) → PACK_GENERATING."""
        FullProductionService(provider=self.provider, storage=self.storage).start(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        report = self.qc.start_qc(order=self.order)
        checklist = {c: True for c in HUMAN_CRITERIA}
        checklist["likeness_face"] = False
        failed = self.qc.finalize_report(report=report, checklist=checklist)
        self.assertEqual(failed.status, QcReport.Status.FAILED)
        self.post("core_order_qc_retry", slot)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        failed.refresh_from_db()
        return failed

    def current_by_slot(self):
        return {str(a.slot_key): a.pk for a in QcService.current_final_assets(self.order)}

    def full_jobs(self):
        return GenerationJob.objects.filter(order=self.order, task_type=GenerationJob.TaskType.FULL).count()


class PendingRetrySlotsTests(QcRetryConsoleTestCase):
    def test_pending_until_the_slot_gets_a_new_current_asset(self):
        failed = self.produce_and_fail_qc_on("hello")
        self.assertEqual([s["slot_key"] for s in failed.retry_slots], ["hello"])
        self.assertEqual(self.qc.pending_retry_slots(self.order), ["hello"])

        FullProductionService(provider=self.provider, storage=self.storage).regenerate_slots(
            order=self.order, slot_keys=["hello"], max_slots=None
        )
        self.assertEqual(self.qc.pending_retry_slots(self.order), [])

    def test_no_pending_without_failed_report(self):
        self.assertEqual(self.qc.pending_retry_slots(self.order), [])


class ConsoleRoutingTests(QcRetryConsoleTestCase):
    def test_regenerate_form_is_prefilled_from_retry_slots(self):
        self.produce_and_fail_qc_on("hello")
        response = self.client.get(self.url("core_order_regenerate_slots"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["slot_keys_value"], "hello")
        # an explicit ?slots= still wins
        response = self.client.get(self.url("core_order_regenerate_slots") + "?slots=bye")
        self.assertEqual(response.context["slot_keys_value"], "bye")

    def test_change_page_offers_regenerate_for_the_pending_slot(self):
        self.produce_and_fail_qc_on("hello")
        response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        content = response.content.decode()
        self.assertIn("на доработке после QC", content)
        self.assertIn(self.url("core_order_regenerate_slots") + "?slots=hello", content)
        self.assertIn("Ждёт перегенерации после QC: «Привет»", content)
        # «Следующий шаг» points at the same regeneration
        self.assertIn("Перегенерировать слот «Привет»", content)

    def test_retry_failed_with_pending_retry_regenerates_instead_of_reentering_qc(self):
        self.produce_and_fail_qc_on("hello")
        before = self.current_by_slot()
        jobs_before = self.full_jobs()
        self.provider.requests.clear()

        messages = self.post("core_order_retry_failed_production")

        self.assertEqual(self.provider.requests, ["hello"])
        self.assertEqual(self.full_jobs(), jobs_before + 1)
        after = self.current_by_slot()
        self.assertNotEqual(after["hello"], before["hello"])
        self.assertEqual(after["bye"], before["bye"])
        self.assertTrue(any("вместо «Повторить неудавшиеся слоты»" in m for m in messages), messages)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        # a fresh attempt on the NEW asset set is allowed
        second = self.qc.start_qc(order=self.order)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(QcReport.objects.filter(order=self.order).count(), 2)

    def test_start_resume_with_pending_retry_regenerates(self):
        self.produce_and_fail_qc_on("bye")
        jobs_before = self.full_jobs()
        self.provider.requests.clear()
        messages = self.post("core_order_start_full_production")
        self.assertEqual(self.provider.requests, ["bye"])
        self.assertEqual(self.full_jobs(), jobs_before + 1)
        self.assertTrue(any("вместо «Запустить производство»" in m for m in messages), messages)

    def test_without_pending_retry_actions_keep_their_normal_semantics(self):
        # Start: real production, one slot per request
        messages = self.post("core_order_start_full_production")
        self.assertTrue(messages[0].startswith("Производство:"), messages)
        self.assertEqual(self.full_jobs(), 1)
        # Retry failed with nothing pending from QC: DRF-2051 semantics
        # untouched (plain plan message, succeeded slot never regenerated)
        messages = self.post("core_order_retry_failed_production")
        self.assertTrue(messages[0].startswith("Производство:"), messages)
        self.assertFalse(any("вместо «" in m for m in messages))
        self.assertEqual(
            sorted(GenerationJob.objects.filter(order=self.order, task_type=GenerationJob.TaskType.FULL).values_list("slot_key", flat=True)),
            sorted(set(self.provider.requests)),
        )


class QcStartGuardTests(QcRetryConsoleTestCase):
    def test_service_refuses_new_report_on_unchanged_set(self):
        self.produce_and_fail_qc_on("hello")
        # simulate the old console path: retry_failed re-enters QC unchanged
        FullProductionService(provider=self.provider, storage=self.storage).retry_failed(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        with self.assertRaisesMessage(QcError, "Nothing was regenerated since QC attempt 1 FAIL"):
            self.qc.start_qc(order=self.order)
        self.assertEqual(QcReport.objects.filter(order=self.order).count(), 1)

    def test_console_refuses_new_report_on_unchanged_set_and_recovers_via_retry(self):
        self.produce_and_fail_qc_on("hello")
        FullProductionService(provider=self.provider, storage=self.storage).retry_failed(order=self.order, max_slots=None)
        messages = self.post("core_order_qc_start")
        self.assertTrue(any("ничего не перегенерировано" in m for m in messages), messages)
        self.assertEqual(QcReport.objects.filter(order=self.order).count(), 1)
        # recovery: QC retry again → PACK_GENERATING → Regenerate (prefilled) → attempt 2
        self.post("core_order_qc_retry", "hello")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        self.post("core_order_regenerate_slots", data={"slot_keys": "hello"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)
        messages = self.post("core_order_qc_start")
        self.assertTrue(any("QC-попытка 2" in m for m in messages), messages)
        self.assertEqual(QcReport.objects.filter(order=self.order).count(), 2)

    def test_new_report_allowed_after_any_asset_changed(self):
        self.produce_and_fail_qc_on("hello")
        FullProductionService(provider=self.provider, storage=self.storage).regenerate_slots(
            order=self.order, slot_keys=["hello"], max_slots=None
        )
        self.order.refresh_from_db()
        report = self.qc.start_qc(order=self.order)
        self.assertEqual(report.attempt, 2)

    def test_failed_report_without_retry_slots_does_not_block(self):
        FullProductionService(provider=self.provider, storage=self.storage).start(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        report = self.qc.start_qc(order=self.order)
        checklist = {c: True for c in HUMAN_CRITERIA}
        checklist["crop"] = False
        self.qc.finalize_report(report=report, checklist=checklist)
        # operator re-evaluates without retry (e.g. checked the wrong box)
        second = self.qc.start_qc(order=self.order)
        self.assertEqual(second.attempt, 2)


class PanelRenderingTests(QcRetryConsoleTestCase):
    def test_panels_render_markup_not_literal_separators(self):
        self.produce_and_fail_qc_on("hello")
        response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        content = response.content.decode()
        self.assertNotIn("&lt;br&gt;", content)
        self.assertNotIn("&amp;nbsp;", content)
        # panels are present and the buttons are real anchors
        self.assertIn("Что делать сейчас", content)
        self.assertIn("Контроль качества", content)
        self.assertIn('class="button" href="' + self.url("core_order_regenerate_slots"), content)
