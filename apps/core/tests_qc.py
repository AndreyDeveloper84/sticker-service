"""DRF-2052 QC tests.

Built on the DRF-2051 production contract (dev): Order.Status
PACK_GENERATING / QUALITY_CONTROL, GenerationJob.TaskType.FULL,
GeneratedAsset.Kind.FINAL and GeneratedAsset.slot_key (= emotion code).
The current asset of a slot is the FINAL asset of the slot's latest
SUCCEEDED FULL attempt; an order enters QUALITY_CONTROL only when the latest
attempt of every slot has succeeded.
"""

import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    Payment,
    Product,
    QcReport,
    Style,
    User,
)
from apps.core.services.full_production import FullProductionService
from apps.core.services.order_state import OrderStateService
from apps.core.services.qc import CHECK_REASON_CODES, HUMAN_CRITERIA, QcError, QcService
from apps.core.storage import LocalMediaStorage

EMOTIONS = [{"code": f"e{i}", "label": f"E{i}"} for i in range(9)]
PACK_CONFIG = {
    "kind": "pack",
    "quantity": 9,
    "emotion_count": 9,
    "emotions": EMOTIONS,
    "price_minor": 50000,
}
SINGLE_CONFIG = {
    "kind": "single",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": [{"code": "wow", "label": "Wow"}],
    "price_minor": 10000,
}
TRIMMED_PACK_CONFIG = {
    **PACK_CONFIG,
    "quantity": 3,
    "emotion_count": 3,
    "emotions": EMOTIONS[:3],
}
PREVIEW_BYTES = b"approved-preview-bytes"


def make_image(size=(512, 512), mode="RGBA", fmt="PNG", noise=False):
    if noise:
        image = Image.frombytes("RGBA", size, os.urandom(size[0] * size[1] * 4))
    else:
        color = (255, 0, 0, 0) if "A" in mode else (255, 0, 0)
        image = Image.new(mode, size, color)
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def all_pass_checklist():
    return {criterion: True for criterion in HUMAN_CRITERIA}


class StickerProvider:
    """Fake image provider producing QC-valid stickers per slot.

    junk_slots return undecodable bytes (QC FAIL on that slot);
    fail_slots raise (FULL attempt FAILED). requests records slot_keys.
    """

    name = "qc-fake"

    def __init__(self):
        self.junk_slots = set()
        self.fail_slots = set()
        self.requests = []

    def generate_preview(self, request):
        slot = request.metadata["slot_key"]
        self.requests.append(slot)
        if slot in self.fail_slots:
            raise RuntimeError(f"provider error on {slot}")
        content = b"junk" if slot in self.junk_slots else make_image()
        return ImageGenerationResult(content=content)


class QcTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=self.user, channel="telegram", external_user_id="qc-user"
        )
        self.style = Style.objects.create(code="comic", name="Comic")

    def make_order(self, config, status=Order.Status.QUALITY_CONTROL):
        product = Product.objects.create(
            code=f"product-{uuid4().hex[:8]}", name="Product", config=config
        )
        # Fixture shortcut: entry into QUALITY_CONTROL is owned by DRF-2051
        # (see QcFullProductionFlowTests for the real path); unit tests set
        # the status directly.
        return Order.objects.create(
            user=self.user,
            channel_identity=self.identity,
            product=product,
            style=self.style,
            status=status,
            selection={"emotions": [e["code"] for e in config.get("emotions", [])]},
        )

    def add_full_job(self, order, attempt, slot_key, status=GenerationJob.Status.SUCCEEDED):
        return GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
            status=status,
            attempt=attempt,
            slot_key=slot_key,
            provider="fake",
        )

    def add_final_asset(
        self,
        storage,
        order,
        attempt,
        slot_key,
        content=None,
        mime_type="image/png",
        job=None,
    ):
        """A FINAL asset produced by a SUCCEEDED FULL attempt for slot_key."""
        content = make_image() if content is None else content
        key = f"generated/order-{order.pk}/final/{uuid4().hex}.png"
        storage.save(key, BytesIO(content))
        job = job or self.add_full_job(order, attempt, slot_key)
        asset = GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=GeneratedAsset.Kind.FINAL,
            slot_key=slot_key,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=len(content),
        )
        job.output_metadata = {"asset_id": asset.pk}
        job.save(update_fields=["output_metadata", "updated_at"])
        return asset


class QcDomainContractTests(QcTestCase):
    def test_pack_expects_9_and_single_expects_1_via_domain_contract(self):
        pack = self.make_order(PACK_CONFIG)
        single = self.make_order(SINGLE_CONFIG)
        self.assertEqual(QcService.expected_asset_count(pack), 9)
        self.assertEqual(QcService.expected_asset_count(single), 1)

    def test_reason_code_mapping_covers_every_automated_check(self):
        for check_name in (
            "expected_count",
            "file_present",
            "decodable",
            "mime_type",
            "dimensions",
            "alpha_channel",
            "file_size",
        ):
            self.assertIn(check_name, CHECK_REASON_CODES)

    def test_report_attempts_are_unique_per_order(self):
        order = self.make_order(SINGLE_CONFIG)
        QcReport.objects.create(order=order, attempt=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QcReport.objects.create(order=order, attempt=1)

    def test_start_qc_requires_quality_control_status(self):
        service = QcService()
        for status in (
            Order.Status.PREVIEW_REVIEW,
            Order.Status.PACK_GENERATING,
            Order.Status.PAID,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        ):
            order = self.make_order(SINGLE_CONFIG, status=status)
            with self.assertRaises(QcError):
                service.start_qc(order=order)

    def test_start_qc_opens_report_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            report = service.start_qc(order=order)
            self.assertEqual(report.status, QcReport.Status.IN_PROGRESS)
            self.assertEqual(report.expected_count, 3)
            self.assertFalse(report.automated_checks["expected_count"])
            again = service.start_qc(order=order)
            self.assertEqual(again.pk, report.pk)
            self.assertEqual(QcReport.objects.filter(order=order).count(), 1)

    def test_incomplete_set_cannot_pass(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            report = service.start_qc(order=order)
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("incomplete_set", result.reason_codes)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)

    def test_unknown_and_incomplete_checklist_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(SINGLE_CONFIG)
            report = service.start_qc(order=order)
            with self.assertRaises(QcError):
                service.finalize_report(report=report, checklist={"bogus": True})
            with self.assertRaises(QcError):
                service.finalize_report(report=report, checklist={"likeness_face": True})

    def test_human_checklist_failure_persists_reason(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(SINGLE_CONFIG)
            report = service.start_qc(order=order)
            checklist = all_pass_checklist()
            checklist["likeness_face"] = False
            result = service.finalize_report(report=report, checklist=checklist)
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("likeness_face", result.reason_codes)
            persisted = QcReport.objects.get(pk=result.pk)
            self.assertFalse(persisted.human_checklist["likeness_face"]["passed"])
            self.assertIsNotNone(persisted.completed_at)

    def test_repeated_finalize_is_safe_noop(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(SINGLE_CONFIG)
            report = service.start_qc(order=order)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            repeated = service.finalize_report(report=failed, checklist=all_pass_checklist())
            self.assertEqual(repeated.status, QcReport.Status.FAILED)
            self.assertEqual(repeated.completed_at, failed.completed_at)

    def test_retry_guards(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            order = self.make_order(SINGLE_CONFIG)
            report = service.start_qc(order=order)
            # retry only for FAILED reports
            with self.assertRaises(QcError):
                service.request_retry(report=report, slot_keys=["e0"])
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            # empty selection rejected before any state change
            with self.assertRaises(QcError):
                service.request_retry(report=failed, slot_keys=[])
            # unknown slot rejected
            with self.assertRaises(QcError):
                service.request_retry(report=failed, slot_keys=["no-such-slot"])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

    def test_pre_qc_delivery_rejected_domain_level(self):
        service = QcService()
        for status in (
            Order.Status.PAID,
            Order.Status.PREVIEW_REVIEW,
            Order.Status.PACK_GENERATING,
            Order.Status.QUALITY_CONTROL,
            Order.Status.DELIVERED,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        ):
            order = self.make_order(SINGLE_CONFIG, status=status)
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)

    def test_gate_covers_delivery_in_progress_for_resume(self):
        # DRF-2053 re-checks the gate on resume; the delivery-owned state is
        # accepted, but only while the PASS still covers the current set.
        storage = LocalMediaStorage()
        order = self.make_order(SINGLE_CONFIG, status=Order.Status.QUALITY_CONTROL)
        self.add_final_asset(storage, order, 1, "wow")
        service = QcService(storage=storage)
        report = service.start_qc(order=order)
        service.finalize_report(report=report, checklist=all_pass_checklist())
        order.refresh_from_db()
        order.status = Order.Status.DELIVERY_IN_PROGRESS
        order.save(update_fields=["status"])
        self.assertEqual(service.assert_delivery_allowed(order=order).pk, report.pk)
        self.add_final_asset(storage, order, 2, "wow")
        with self.assertRaises(QcError):
            service.assert_delivery_allowed(order=order)

    def test_ready_for_delivery_without_passed_report_is_rejected(self):
        order = self.make_order(SINGLE_CONFIG, status=Order.Status.READY_FOR_DELIVERY)
        with self.assertRaises(QcError):
            QcService().assert_delivery_allowed(order=order)


class QcOrderStateTests(TestCase):
    def test_quality_control_exit_set_is_exactly_qc_outcomes(self):
        # One entry for QUALITY_CONTROL: DRF-2051's FAILED exit plus the
        # DRF-2052 outcomes. A duplicate key (str vs TextChoices hash the
        # same) would silently overwrite one side; the exact set guards it.
        self.assertEqual(
            OrderStateService.allowed_targets(Order.Status.QUALITY_CONTROL),
            {
                Order.Status.READY_FOR_DELIVERY,
                Order.Status.PACK_GENERATING,
                Order.Status.FAILED,
            },
        )
        # DRF-2053 adds the delivery entry to the QC exit state.
        self.assertEqual(
            OrderStateService.allowed_targets(Order.Status.READY_FOR_DELIVERY),
            {Order.Status.DELIVERY_IN_PROGRESS, Order.Status.FAILED},
        )

    def test_transition_keys_are_status_members_without_duplicates(self):
        keys = list(OrderStateService.transitions)
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsInstance(key, Order.Status)
        self.assertEqual(
            sum(1 for key in keys if key == Order.Status.QUALITY_CONTROL), 1
        )

    def test_drf_2051_production_transitions_are_preserved(self):
        self.assertIn(
            Order.Status.PACK_GENERATING,
            OrderStateService.allowed_targets(Order.Status.PREVIEW_REVIEW),
        )
        self.assertEqual(
            OrderStateService.allowed_targets(Order.Status.PACK_GENERATING),
            {Order.Status.QUALITY_CONTROL, Order.Status.FAILED},
        )
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.CANCELLED), set())
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.FAILED), set())


class QcCurrentAssetRuleTests(QcTestCase):
    def test_current_asset_is_latest_succeeded_full_attempt(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow")
            current = self.add_final_asset(storage, order, attempt=2, slot_key="wow")
            # Newer by created_at but attached to a FAILED attempt: not
            # current. A FINAL row under a non-FULL job is not current either.
            failed_job = self.add_full_job(
                order, attempt=3, slot_key="wow", status=GenerationJob.Status.FAILED
            )
            self.add_final_asset(storage, order, attempt=3, slot_key="wow", job=failed_job)
            preview_job = GenerationJob.objects.create(
                order=order,
                task_type=GenerationJob.TaskType.PREVIEW,
                status=GenerationJob.Status.SUCCEEDED,
                attempt=1,
                provider="fake",
            )
            self.add_final_asset(storage, order, attempt=1, slot_key="wow", job=preview_job)

            assets = QcService.current_final_assets(order)
            self.assertEqual([asset.pk for asset in assets], [current.pk])
            self.assertEqual(order.generated_assets.count(), 4)

    def test_slot_without_succeeded_attempt_has_no_current_asset(self):
        order = self.make_order(TRIMMED_PACK_CONFIG)
        self.add_full_job(order, attempt=1, slot_key="e0", status=GenerationJob.Status.FAILED)
        self.assertEqual(QcService.current_final_assets(order), [])


class QcAutomatedChecksTests(QcTestCase):
    def _run_fail(self, storage, order, **asset_kwargs):
        service = QcService(storage=storage)
        asset = self.add_final_asset(storage, order, attempt=1, **asset_kwargs)
        report = service.start_qc(order=order)
        result = service.finalize_report(report=report, checklist=all_pass_checklist())
        return asset, result

    def test_full_set_passes_and_enables_delivery_gate(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            for index, slot in enumerate(["e0", "e1", "e2"]):
                self.add_final_asset(storage, order, attempt=index + 1, slot_key=slot)
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            self.assertEqual(report.slot_keys, ["e0", "e1", "e2"])
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.PASSED)
            self.assertEqual(result.reason_codes, [])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
            gate = service.assert_delivery_allowed(order=order)
            self.assertEqual(gate.pk, result.pk)

    def test_undecodable_asset_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, slot_key="wow", content=b"not-an-image"
            )
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("undecodable_image", result.reason_codes)

    def test_wrong_dimensions_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, slot_key="wow", content=make_image(size=(400, 400))
            )
            self.assertIn("bad_dimensions", result.reason_codes)

    def test_missing_alpha_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, slot_key="wow", content=make_image(mode="RGB")
            )
            self.assertIn("missing_alpha", result.reason_codes)

    def test_mime_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, slot_key="wow", mime_type="image/jpeg"
            )
            self.assertIn("bad_mime_type", result.reason_codes)

    def test_oversized_file_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            content = make_image(noise=True)
            self.assertGreater(len(content), 512 * 1024)
            _asset, result = self._run_fail(
                storage, order, slot_key="wow", content=content
            )
            self.assertIn("file_too_large", result.reason_codes)

    def test_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            asset = self.add_final_asset(storage, order, attempt=1, slot_key="wow")
            Path(storage.root / asset.storage_key).unlink()
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertIn("missing_file", result.reason_codes)


class QcRetryAndGateTests(QcTestCase):
    def test_retry_selected_slots_only(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="e0")
            bad = self.add_final_asset(
                storage, order, attempt=2, slot_key="e1", content=b"junk"
            )
            self.add_final_asset(storage, order, attempt=3, slot_key="e2")
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(failed.status, QcReport.Status.FAILED)

            updated = service.request_retry(report=failed, slot_keys=["e1"])
            self.assertEqual(len(updated.retry_slots), 1)
            slot = updated.retry_slots[0]
            # canonical retry identity is slot_key; asset_id is audit only
            self.assertEqual(slot["slot_key"], "e1")
            self.assertEqual(slot["asset_id"], bad.pk)
            self.assertIn("undecodable_image", slot["reason_codes"])
            self.assertNotIn("e0", {s["slot_key"] for s in updated.retry_slots})
            self.assertNotIn("e2", {s["slot_key"] for s in updated.retry_slots})
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)

    def test_regenerated_slot_replaces_asset_by_slot_key(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow", content=b"junk")
            # DRF-2051 re-generation: a new SUCCEEDED attempt for the same
            # slot_key; its asset becomes current, the old one stays for audit.
            self.add_final_asset(storage, order, attempt=2, slot_key="wow")
            assets = QcService.current_final_assets(order)
            self.assertEqual(len(assets), 1)
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.PASSED)

    def test_delivery_gate_invalidated_when_slots_change_after_pass(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow")
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            service.finalize_report(report=report, checklist=all_pass_checklist())
            order.refresh_from_db()
            service.assert_delivery_allowed(order=order)
            # Same slot_key, new current asset -> the PASS no longer covers
            # the current set.
            self.add_final_asset(storage, order, attempt=2, slot_key="wow")
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)

    def test_retry_then_repeated_qc_opens_new_attempt(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow", content=b"junk")
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            service.request_retry(report=failed, slot_keys=["wow"])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)
            # QC cannot reopen while production is running.
            with self.assertRaises(QcError):
                service.start_qc(order=order)
            OrderStateService.transition(order=order, to_status=Order.Status.QUALITY_CONTROL)
            second = service.start_qc(order=order)
            self.assertEqual(second.attempt, 2)


class QcFullProductionFlowTests(QcTestCase):
    """End-to-end with the real DRF-2051 FullProductionService."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self._override = override_settings(MEDIA_ROOT=Path(self._root.name))
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.storage = LocalMediaStorage()
        self.provider = StickerProvider()
        self.production = FullProductionService(provider=self.provider, storage=self.storage)
        self.qc = QcService(storage=self.storage)

    def make_production_order(self, config=TRIMMED_PACK_CONFIG):
        order = self.make_order(config, status=Order.Status.PREVIEW_REVIEW)
        job = GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED,
            attempt=1,
            provider="fake",
        )
        preview_key = f"generated/order-{order.pk}/preview/job-{job.pk}.png"
        self.storage.save(preview_key, BytesIO(PREVIEW_BYTES))
        GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=preview_key,
            size_bytes=len(PREVIEW_BYTES),
            metadata={"internal_approved": True, "customer_approved": True},
        )
        Payment.objects.create(
            order=order,
            provider="telegram_stars",
            status=Payment.Status.CONFIRMED,
            amount_minor=50000,
            currency="RUB",
            confirmed_at=timezone.now(),
        )
        return order

    def current_by_slot(self, order):
        return {str(a.slot_key): a.pk for a in QcService.current_final_assets(order)}

    def fail_qc_on_slot(self, order, slot):
        """FULL -> QUALITY_CONTROL -> QC FAIL(slot) -> request_retry(slot)."""
        self.provider.junk_slots = {slot}
        self.production.start(order=order, max_slots=None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        before = self.current_by_slot(order)
        self.assertEqual(set(before), {"e0", "e1", "e2"})

        report = self.qc.start_qc(order=order)
        failed = self.qc.finalize_report(report=report, checklist=all_pass_checklist())
        self.assertEqual(failed.status, QcReport.Status.FAILED)
        self.assertIn("undecodable_image", failed.reason_codes)
        self.assertFalse(failed.automated_checks[slot]["decodable"])
        for other in set(before) - {slot}:
            checks = failed.automated_checks[other]
            self.assertTrue(all(ok for name, ok in checks.items() if name != "asset_id"))

        self.qc.request_retry(report=failed, slot_keys=[slot])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        self.provider.junk_slots = set()
        self.provider.requests.clear()
        return before, failed

    def test_qc_fail_regenerate_slot_qc_pass_ready_for_delivery(self):
        order = self.make_production_order()
        before, _failed = self.fail_qc_on_slot(order, "e1")

        self.production.regenerate_slots(order=order, slot_keys=["e1"], max_slots=None)

        # Only the requested slot was regenerated: one new FULL attempt, one
        # new FINAL asset, other slots keep their current assets.
        self.assertEqual(self.provider.requests, ["e1"])
        full_jobs = GenerationJob.objects.filter(
            order=order, task_type=GenerationJob.TaskType.FULL
        )
        self.assertEqual(full_jobs.count(), 4)
        self.assertEqual(
            order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL).count(), 4
        )
        after = self.current_by_slot(order)
        self.assertEqual(after["e0"], before["e0"])
        self.assertEqual(after["e2"], before["e2"])
        self.assertNotEqual(after["e1"], before["e1"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

        second = self.qc.start_qc(order=order)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(second.slot_keys, ["e0", "e1", "e2"])
        self.assertEqual(second.asset_ids, [after["e0"], after["e1"], after["e2"]])
        self.assertTrue(second.automated_checks["expected_count"])
        passed = self.qc.finalize_report(report=second, checklist=all_pass_checklist())
        self.assertEqual(passed.status, QcReport.Status.PASSED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
        self.assertEqual(self.qc.assert_delivery_allowed(order=order).pk, passed.pk)

    def test_failed_regeneration_does_not_return_order_to_qc_with_old_asset(self):
        order = self.make_production_order()
        before, _failed = self.fail_qc_on_slot(order, "e1")

        self.provider.fail_slots = {"e1"}
        self.production.regenerate_slots(order=order, slot_keys=["e1"], max_slots=None)

        # The slot's latest attempt FAILED: the order stays in production
        # even though an older (QC-rejected) FINAL asset for e1 exists.
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        plan = {slot.slot_key: slot for slot in self.production.production_plan(order)}
        self.assertEqual(plan["e1"].status, "failed")
        self.assertEqual(plan["e1"].asset_id, before["e1"])
        self.assertEqual(self.current_by_slot(order), before)
        with self.assertRaises(QcError):
            self.qc.start_qc(order=order)
        with self.assertRaises(QcError):
            self.qc.assert_delivery_allowed(order=order)

        # Recovery: retry_failed regenerates e1 only, then QC re-enters
        # with the new asset.
        self.provider.fail_slots = set()
        self.provider.requests.clear()
        self.production.retry_failed(order=order, max_slots=None)
        self.assertEqual(self.provider.requests, ["e1"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        after = self.current_by_slot(order)
        self.assertNotEqual(after["e1"], before["e1"])
        self.assertEqual(
            {k: v for k, v in after.items() if k != "e1"},
            {k: v for k, v in before.items() if k != "e1"},
        )
        report = self.qc.start_qc(order=order)
        self.assertEqual(report.asset_ids, [after["e0"], after["e1"], after["e2"]])


class QcConsoleTests(QcTestCase):
    def setUp(self):
        super().setUp()
        self.operator = get_user_model().objects.create_superuser(
            username="operator", password="pw", email="op@example.com"
        )
        self.client.force_login(self.operator)

    def test_order_page_shows_qc_panel_and_gate_blocked(self):
        order = self.make_order(SINGLE_CONFIG)
        response = self.client.get(reverse("admin:core_order_change", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Ожидается: 1 · Готово: 0", content)
        self.assertIn("DELIVERY: заблокирован", content)

    def test_console_start_and_finalize_fail_flow(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            order = self.make_order(SINGLE_CONFIG)
            start_url = reverse("admin:core_order_qc_start", args=[order.pk])
            self.assertEqual(self.client.get(start_url).status_code, 200)
            response = self.client.post(start_url, follow=True)
            self.assertEqual(response.status_code, 200)
            report = QcReport.objects.get(order=order)
            self.assertEqual(report.status, QcReport.Status.IN_PROGRESS)

            finalize_url = reverse("admin:core_order_qc_finalize", args=[order.pk])
            self.assertEqual(self.client.get(finalize_url).status_code, 200)
            # no final assets yet -> even a fully ticked checklist cannot PASS
            response = self.client.post(
                finalize_url,
                {criterion: "1" for criterion in HUMAN_CRITERIA},
                follow=True,
            )
            self.assertEqual(response.status_code, 200)
            report.refresh_from_db()
            self.assertEqual(report.status, QcReport.Status.FAILED)
            self.assertIn("incomplete_set", report.reason_codes)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

    def test_console_start_rejected_outside_quality_control(self):
        order = self.make_order(SINGLE_CONFIG, status=Order.Status.PREVIEW_REVIEW)
        start_url = reverse("admin:core_order_qc_start", args=[order.pk])
        response = self.client.post(start_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(QcReport.objects.filter(order=order).count(), 0)

    def test_console_fail_shows_slot_retry_and_blocked_delivery(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow", content=b"junk")
            service = QcService(storage=storage)
            report = service.start_qc(order=order)
            service.finalize_report(report=report, checklist=all_pass_checklist())
            response = self.client.get(reverse("admin:core_order_change", args=[order.pk]))
            content = response.content.decode()
            self.assertIn("undecodable_image", content)
            self.assertIn("Retry этот slot", content)
            self.assertIn("DELIVERY: заблокирован", content)

            retry_url = reverse("admin:core_order_qc_retry", args=[order.pk, "wow"])
            self.assertEqual(self.client.get(retry_url).status_code, 200)
            self.client.post(retry_url, follow=True)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)
