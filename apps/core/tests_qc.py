import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
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
from apps.core.services.qc import HUMAN_CRITERIA, QcError, QcService
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

    def make_order(self, config, status=Order.Status.PACK_GENERATING):
        product = Product.objects.create(
            code=f"product-{uuid4().hex[:8]}", name="Product", config=config
        )
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
        content=None,
        emotion="e0",
        mime_type="image/png",
        metadata=None,
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
            kind=GeneratedAsset.Kind.FINAL,
            storage_key=key,
            mime_type=mime_type,
            size_bytes=len(content),
            metadata={"emotion": emotion, **(metadata or {})},
        )


class QcDomainContractTests(QcTestCase):
    def test_pack_expects_9_and_single_expects_1_via_domain_contract(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            pack = self.make_order(PACK_CONFIG)
            single = self.make_order(SINGLE_CONFIG)
            self.assertEqual(QcService.expected_asset_count(pack), 9)
            self.assertEqual(QcService.expected_asset_count(single), 1)

    def test_full_pack_set_passes(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            for index, emotion in enumerate(["e0", "e1", "e2"]):
                self.add_final_asset(storage, order, attempt=index + 1, emotion=emotion)
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
            self.assertEqual(report.expected_count, 3)
            self.assertEqual(len(report.asset_ids), 3)

            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.PASSED)
            self.assertEqual(result.reason_codes, [])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
            gate = service.assert_delivery_allowed(order=order)
            self.assertEqual(gate.pk, result.pk)

    def test_incomplete_set_cannot_pass(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            self.add_final_asset(storage, order, attempt=1, emotion="e0")
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("incomplete_set", result.reason_codes)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)


class QcAutomatedChecksTests(QcTestCase):
    def _run_fail(self, storage, order, **asset_kwargs):
        service = QcService(storage=storage)
        asset = self.add_final_asset(storage, order, attempt=1, **asset_kwargs)
        report = service.submit_for_qc(order=order)
        result = service.finalize_report(report=report, checklist=all_pass_checklist())
        return asset, result

    def test_undecodable_asset_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(storage, order, content=b"not-an-image")
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("undecodable_image", result.reason_codes)

    def test_wrong_dimensions_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, content=make_image(size=(400, 400))
            )
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("bad_dimensions", result.reason_codes)

    def test_missing_alpha_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, content=make_image(mode="RGB")
            )
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("missing_alpha", result.reason_codes)

    def test_mime_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(
                storage, order, content=make_image(), mime_type="image/jpeg"
            )
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("bad_mime_type", result.reason_codes)

    def test_oversized_file_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            content = make_image(noise=True)
            self.assertGreater(len(content), 512 * 1024)
            _asset, result = self._run_fail(storage, order, content=content)
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("file_too_large", result.reason_codes)

    def test_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            asset = self.add_final_asset(storage, order, attempt=1)
            Path(storage.root / asset.storage_key).unlink()
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            result = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertIn("missing_file", result.reason_codes)

    def test_fail_reason_persisted(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            _asset, result = self._run_fail(storage, order, content=b"junk")
            persisted = QcReport.objects.get(pk=result.pk)
            self.assertEqual(persisted.status, QcReport.Status.FAILED)
            self.assertIn("undecodable_image", persisted.reason_codes)
            self.assertIsNotNone(persisted.completed_at)

    def test_human_checklist_failure_persists_reason(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1)
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            checklist = all_pass_checklist()
            checklist["likeness_face"] = False
            result = service.finalize_report(report=report, checklist=checklist)
            self.assertEqual(result.status, QcReport.Status.FAILED)
            self.assertEqual(result.reason_codes, ["likeness_face"])
            self.assertFalse(result.human_checklist["likeness_face"]["passed"])

    def test_incomplete_checklist_rejected(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1)
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            with self.assertRaises(QcError):
                service.finalize_report(report=report, checklist={"likeness_face": True})


class QcRetryAndGateTests(QcTestCase):
    def test_retry_selected_only(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(TRIMMED_PACK_CONFIG)
            good1 = self.add_final_asset(storage, order, attempt=1, emotion="e0")
            bad = self.add_final_asset(
                storage, order, attempt=2, emotion="e1", content=b"junk"
            )
            good2 = self.add_final_asset(storage, order, attempt=3, emotion="e2")
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            self.assertEqual(failed.status, QcReport.Status.FAILED)

            updated = service.request_retry(report=failed, asset_ids=[bad.pk])
            self.assertEqual(len(updated.retry_slots), 1)
            slot = updated.retry_slots[0]
            self.assertEqual(slot["asset_id"], bad.pk)
            self.assertEqual(slot["emotion"], "e1")
            self.assertIn("undecodable_image", slot["reason_codes"])
            untouched = {good1.pk, good2.pk}
            self.assertFalse(untouched & {s["asset_id"] for s in updated.retry_slots})
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)

    def test_retry_rejects_non_current_asset(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            asset = self.add_final_asset(storage, order, attempt=1, content=b"junk")
            other_order = self.make_order(SINGLE_CONFIG)
            other = self.add_final_asset(storage, other_order, attempt=1)
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            with self.assertRaises(QcError):
                service.request_retry(report=failed, asset_ids=[other.pk])
            # Superseded assets are not retryable as current slots.
            asset.metadata = {**asset.metadata, "superseded": True}
            asset.save(update_fields=["metadata"])
            with self.assertRaises(QcError):
                service.request_retry(report=failed, asset_ids=[asset.pk])

    def test_pre_qc_delivery_rejected_domain_level(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            order = self.make_order(SINGLE_CONFIG, status=Order.Status.PACK_GENERATING)
            with self.assertRaises(QcError):
                QcService().assert_delivery_allowed(order=order)
            order.status = Order.Status.PREVIEW_REVIEW
            with self.assertRaises(QcError):
                QcService().assert_delivery_allowed(order=order)

    def test_delivery_gate_invalidated_when_assets_change_after_pass(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            asset = self.add_final_asset(storage, order, attempt=1)
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            service.finalize_report(report=report, checklist=all_pass_checklist())
            order.refresh_from_db()
            service.assert_delivery_allowed(order=order)
            asset.metadata = {**asset.metadata, "superseded": True}
            asset.save(update_fields=["metadata"])
            with self.assertRaises(QcError):
                service.assert_delivery_allowed(order=order)

    def test_repeated_qc_is_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1, content=b"junk")
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            # submit again while QC is open: same report, no duplicate attempt
            again = service.submit_for_qc(order=order)
            self.assertEqual(again.pk, report.pk)
            failed = service.finalize_report(report=report, checklist=all_pass_checklist())
            # repeated finalize on a completed report is a no-op
            repeated = service.finalize_report(report=failed, checklist=all_pass_checklist())
            self.assertEqual(repeated.status, QcReport.Status.FAILED)
            self.assertEqual(QcReport.objects.filter(order=order).count(), 1)
            # after retry the order returns to production; a new QC round opens attempt 2
            service.request_retry(report=failed, asset_ids=failed.asset_ids)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)
            second = service.submit_for_qc(order=order)
            self.assertEqual(second.attempt, 2)

    def test_terminal_guards(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            service = QcService(storage=LocalMediaStorage(root=Path(root)))
            for status in (Order.Status.CANCELLED, Order.Status.FAILED):
                order = self.make_order(SINGLE_CONFIG, status=status)
                with self.assertRaises(QcError):
                    service.submit_for_qc(order=order)
                with self.assertRaises(QcError):
                    service.assert_delivery_allowed(order=order)


class QcConsoleTests(QcTestCase):
    def setUp(self):
        super().setUp()
        self.operator = get_user_model().objects.create_superuser(
            username="operator", password="pw", email="op@example.com"
        )
        self.client.force_login(self.operator)

    def test_order_page_shows_qc_panel_and_gate_blocked(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1)
            response = self.client.get(reverse("admin:core_order_change", args=[order.pk]))
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            self.assertIn("Ожидается: 1 · Готово: 1", content)
            self.assertIn("DELIVERY: заблокирован", content)

    def test_console_submit_finalize_pass_flow(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            self.add_final_asset(storage, order, attempt=1)
            submit_url = reverse("admin:core_order_qc_submit", args=[order.pk])
            self.assertEqual(self.client.get(submit_url).status_code, 200)
            response = self.client.post(submit_url, follow=True)
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

            finalize_url = reverse("admin:core_order_qc_finalize", args=[order.pk])
            self.assertEqual(self.client.get(finalize_url).status_code, 200)
            response = self.client.post(
                finalize_url,
                {criterion: "1" for criterion in HUMAN_CRITERIA},
                follow=True,
            )
            self.assertEqual(response.status_code, 200)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
            report = QcReport.objects.get(order=order)
            self.assertEqual(report.status, QcReport.Status.PASSED)

    def test_console_fail_shows_retry_and_blocked_delivery(self):
        with tempfile.TemporaryDirectory() as root, override_settings(MEDIA_ROOT=Path(root)):
            storage = LocalMediaStorage(root=Path(root))
            order = self.make_order(SINGLE_CONFIG)
            asset = self.add_final_asset(storage, order, attempt=1, content=b"junk")
            service = QcService(storage=storage)
            report = service.submit_for_qc(order=order)
            service.finalize_report(report=report, checklist=all_pass_checklist())
            response = self.client.get(reverse("admin:core_order_change", args=[order.pk]))
            content = response.content.decode()
            self.assertIn("undecodable_image", content)
            self.assertIn("Retry этот asset", content)
            self.assertIn("DELIVERY: заблокирован", content)

            retry_url = reverse("admin:core_order_qc_retry", args=[order.pk, asset.pk])
            self.assertEqual(self.client.get(retry_url).status_code, 200)
            self.client.post(retry_url, follow=True)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)
