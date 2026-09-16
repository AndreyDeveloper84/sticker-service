"""DRF-2052 QC tests.

Canonical production contract (Order statuses PACK_GENERATING /
QUALITY_CONTROL, GenerationJob.TaskType.FULL, GeneratedAsset.Kind.FINAL,
GeneratedAsset.slot_key) is owned by DRF-2051. DRF-2052 does not create a
shadow/temporary production schema: tests that need the canonical slot_key
column are skip-guarded and activate automatically once DRF-2051 lands in
dev. Tests that exercise pure QC ownership run today.
"""

import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    Product,
    QcReport,
    Style,
    User,
)
from apps.core.services.order_state import (
    PACK_GENERATING,
    QUALITY_CONTROL,
    OrderStateService,
)
from apps.core.services.qc import (
    CHECK_REASON_CODES,
    FINAL_ASSET_KIND,
    HUMAN_CRITERIA,
    QcError,
    QcService,
)
from apps.core.storage import LocalMediaStorage

HAS_CANONICAL_SLOT_KEY = any(
    field.name == "slot_key" for field in GeneratedAsset._meta.get_fields()
)
requires_drf_2051 = unittest.skipUnless(
    HAS_CANONICAL_SLOT_KEY,
    "canonical production contract (GeneratedAsset.slot_key) lands with DRF-2051",
)

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
TRIMMED_PACK_CONFIG = {**PACK_CONFIG, "emotion_count": 3, "emotions": EMOTIONS[:3]}


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


class QcTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=self.user, channel="telegram", external_user_id="qc-user"
        )
        self.style = Style.objects.create(code="comic", name="Comic")

    def make_order(self, config, status=QUALITY_CONTROL):
        product = Product.objects.create(
            code=f"product-{uuid4().hex[:8]}", name="Product", config=config
        )
        # Fixture shortcut: entry into QUALITY_CONTROL is owned by DRF-2051;
        # tests set the status directly instead of re-defining production.
        return Order.objects.create(
            user=self.user,
            channel_identity=self.identity,
            product=product,
            style=self.style,
            status=status,
            selection={"emotions": [e["code"] for e in config.get("emotions", [])]},
        )

    def add_final_asset(
        self,
        storage,
        order,
        attempt,
        slot_key,
        content=None,
        mime_type="image/png",
    ):
        content = make_image() if content is None else content
        key = f"generated/order-{order.pk}/final/{uuid4().hex}.png"
        storage.save(key, BytesIO(content))
        job = GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED,
            attempt=attempt,
            provider="fake",
        )
        return GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=FINAL_ASSET_KIND,
            slot_key=slot_key,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=len(content),
        )


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
        from django.db import transaction

        order = self.make_order(SINGLE_CONFIG)
        QcReport.objects.create(order=order, attempt=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QcReport.objects.create(order=order, attempt=1)

    def test_start_qc_requires_quality_control_status(self):
        service = QcService()
        for status in (
            Order.Status.PREVIEW_REVIEW,
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
            self.assertEqual(order.status, QUALITY_CONTROL)
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

    def test_pre_qc_delivery_rejected_domain_level(self):
        service = QcService()
        for status in (
            Order.Status.PAID,
            Order.Status.PREVIEW_REVIEW,
            PACK_GENERATING,
            QUALITY_CONTROL,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        ):
            order = self.make_order(SINGLE_CONFIG, status=status)
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)

    def test_ready_for_delivery_without_passed_report_is_rejected(self):
        order = self.make_order(SINGLE_CONFIG, status=Order.Status.READY_FOR_DELIVERY)
        with self.assertRaises(QcError):
            QcService().assert_delivery_allowed(order=order)

    def test_qc_outcome_transitions_are_registered(self):
        self.assertIn(Order.Status.READY_FOR_DELIVERY, OrderStateService.allowed_targets(QUALITY_CONTROL))
        self.assertIn(PACK_GENERATING, OrderStateService.allowed_targets(QUALITY_CONTROL))
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.CANCELLED), set())
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.FAILED), set())


@requires_drf_2051
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


@requires_drf_2051
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
            self.assertEqual(order.status, PACK_GENERATING)

    def test_regenerated_slot_replaces_asset_by_slot_key(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, slot_key="wow", content=b"junk")
            # DRF-2051 re-generation writes a new asset under the same slot_key
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
            self.assertEqual(order.status, PACK_GENERATING)
            # DRF-2051 re-enters QUALITY_CONTROL after re-generation
            order.status = QUALITY_CONTROL
            order.save(update_fields=["status"])
            second = service.start_qc(order=order)
            self.assertEqual(second.attempt, 2)


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
            self.assertEqual(order.status, QUALITY_CONTROL)

    def test_console_start_rejected_outside_quality_control(self):
        order = self.make_order(SINGLE_CONFIG, status=Order.Status.PREVIEW_REVIEW)
        start_url = reverse("admin:core_order_qc_start", args=[order.pk])
        response = self.client.post(start_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(QcReport.objects.filter(order=order).count(), 0)

    @requires_drf_2051
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
            self.assertEqual(order.status, PACK_GENERATING)
