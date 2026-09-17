"""DRF-2076: sticker format normalization before automated QC checks.

The image API cannot return a 512 px side (minimum size is 1024), so every
current FINAL asset is fitted into the 512x512 sticker box when a QC attempt
opens. Normalization is in place on the same GeneratedAsset row (pk, job,
slot_key unchanged); the provider original stays under its old storage_key
and is referenced from metadata["normalized_from"]. Alpha is preserved but
never invented; the step is idempotent and env-gated
(QC_NORMALIZE_FINAL_ASSETS=0 disables it).
"""

import os
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from PIL import Image

from apps.core.models import GeneratedAsset, Order, QcReport
from apps.core.services.final_delivery import FinalDeliveryService
from apps.core.services.qc import (
    MAX_FILE_BYTES,
    NORMALIZE_ENV,
    NORMALIZED_KEY,
    QcError,
    QcService,
    normalization_enabled,
)
from apps.core.storage import LocalMediaStorage
from apps.core.tests_final_delivery import FakeAdapter
from apps.core.tests_qc import SINGLE_CONFIG, TRIMMED_PACK_CONFIG, QcTestCase, all_pass_checklist, make_image


def opaque_noise(size=(1024, 1024), fmt="PNG", **save):
    """Opaque, incompressible image (real photo-like size) in the given format."""
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buffer = BytesIO()
    image.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def alpha_noise(size=(1024, 1024), fmt="PNG", **save):
    image = Image.frombytes("RGBA", size, os.urandom(size[0] * size[1] * 4))
    buffer = BytesIO()
    image.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def decode(storage, key):
    with storage.open(key, "rb") as source:
        content = source.read()
    image = Image.open(BytesIO(content))
    image.load()
    return image, content


class QcNormalizeTestCase(QcTestCase):
    def setUp(self):
        super().setUp()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self._override = override_settings(MEDIA_ROOT=Path(self._root.name))
        self._override.enable()
        self.addCleanup(self._override.disable)
        env = mock.patch.dict(os.environ, {NORMALIZE_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)
        self.storage = LocalMediaStorage()
        self.qc = QcService(storage=self.storage)

    def single_order_with(self, content, mime_type="image/png"):
        order = self.make_order(SINGLE_CONFIG)
        asset = self.add_final_asset(self.storage, order, 1, "wow", content=content, mime_type=mime_type)
        return order, asset


class NormalizationTests(QcNormalizeTestCase):
    def test_1024_rgba_png_becomes_512_png_and_passes(self):
        content = make_image(size=(1024, 1024), mode="RGBA")
        order, asset = self.single_order_with(content)
        original_key = asset.storage_key

        report = self.qc.start_qc(order=order)
        asset.refresh_from_db()

        self.assertNotEqual(asset.storage_key, original_key)
        self.assertEqual(asset.mime_type, "image/png")
        image, stored = decode(self.storage, asset.storage_key)
        self.assertEqual(image.size, (512, 512))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.format, "PNG")
        self.assertEqual(asset.size_bytes, len(stored))
        self.assertLessEqual(asset.size_bytes, MAX_FILE_BYTES)
        # original retained and described
        self.assertTrue(self.storage.exists(original_key))
        origin = asset.metadata[NORMALIZED_KEY]
        self.assertEqual(origin["storage_key"], original_key)
        self.assertEqual((origin["width"], origin["height"], origin["mode"], origin["format"]), (1024, 1024, "RGBA", "PNG"))
        self.assertEqual(origin["size_bytes"], len(content))
        self.assertEqual(origin["version"], 1)
        # automated checks now pass on the same asset row
        checks = report.automated_checks["wow"]
        self.assertEqual(checks["asset_id"], asset.pk)
        self.assertTrue(checks["dimensions"] and checks["alpha_channel"] and checks["file_size"] and checks["mime_type"])
        passed = self.qc.finalize_report(report=report, checklist=all_pass_checklist())
        self.assertEqual(passed.status, QcReport.Status.PASSED)

    def test_landscape_1536x1024_fits_to_512x341(self):
        order, asset = self.single_order_with(make_image(size=(1536, 1024), mode="RGBA"))
        report = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        image, _ = decode(self.storage, asset.storage_key)
        self.assertEqual(image.size, (512, 341))
        self.assertTrue(report.automated_checks["wow"]["dimensions"])

    def test_portrait_1024x1536_fits_to_341x512(self):
        order, asset = self.single_order_with(make_image(size=(1024, 1536), mode="RGBA"))
        self.qc.start_qc(order=order)
        asset.refresh_from_db()
        image, _ = decode(self.storage, asset.storage_key)
        self.assertEqual(image.size, (341, 512))

    def test_webp_input_is_normalized_and_passes(self):
        content = make_image(size=(1024, 1024), mode="RGBA", fmt="WEBP")
        order, asset = self.single_order_with(content, mime_type="image/webp")
        report = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        image, _ = decode(self.storage, asset.storage_key)
        self.assertEqual(image.size, (512, 512))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(asset.metadata[NORMALIZED_KEY]["format"], "WEBP")
        self.assertEqual(asset.metadata[NORMALIZED_KEY]["mime_type"], "image/webp")
        checks = report.automated_checks["wow"]
        self.assertTrue(checks["dimensions"] and checks["alpha_channel"] and checks["mime_type"])

    def test_opaque_sources_are_resized_but_alpha_is_never_invented(self):
        for label, content, mime in (
            ("jpeg", opaque_noise(fmt="JPEG", quality=90), "image/jpeg"),
            ("rgb-png", make_image(size=(1024, 1024), mode="RGB"), "image/png"),
        ):
            with self.subTest(label):
                order, asset = self.single_order_with(content, mime_type=mime)
                report = self.qc.start_qc(order=order)
                asset.refresh_from_db()
                image, _ = decode(self.storage, asset.storage_key)
                self.assertEqual(image.size, (512, 512))
                self.assertEqual(image.mode, "RGB")
                self.assertIn(asset.mime_type, ("image/png", "image/webp"))
                checks = report.automated_checks["wow"]
                self.assertTrue(checks["dimensions"], label)
                self.assertTrue(checks["mime_type"], label)  # re-encoded as png/webp
                self.assertFalse(checks["alpha_channel"], label)
                failed = self.qc.finalize_report(report=report, checklist=all_pass_checklist())
                self.assertEqual(failed.status, QcReport.Status.FAILED)
                self.assertEqual(failed.reason_codes, ["missing_alpha"])

    def test_large_alpha_source_falls_back_to_webp_under_limit(self):
        # Random RGBA does not compress: 512x512 PNG ~1 MB > 512 KB → WebP.
        order, asset = self.single_order_with(alpha_noise())
        report = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        self.assertEqual(asset.mime_type, "image/webp")
        image, stored = decode(self.storage, asset.storage_key)
        self.assertEqual(image.format, "WEBP")
        self.assertEqual(image.size, (512, 512))
        self.assertEqual(image.mode, "RGBA")
        self.assertLessEqual(len(stored), MAX_FILE_BYTES)
        checks = report.automated_checks["wow"]
        self.assertTrue(checks["file_size"] and checks["mime_type"] and checks["alpha_channel"])

    def test_idempotent_second_attempt_keeps_normalized_file(self):
        order, asset = self.single_order_with(make_image(size=(1024, 1024), mode="RGBA"))
        first = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        normalized_key = asset.storage_key
        origin = dict(asset.metadata[NORMALIZED_KEY])
        # FAIL on a human criterion → retry → new attempt on the same asset
        checklist = all_pass_checklist()
        checklist["crop"] = False
        self.qc.finalize_report(report=first, checklist=checklist)
        # the order stays in QUALITY_CONTROL on a human-only FAIL; open attempt 2
        second = self.qc.start_qc(order=order)
        self.assertEqual(second.attempt, 2)
        asset.refresh_from_db()
        self.assertEqual(asset.storage_key, normalized_key)
        self.assertEqual(asset.metadata[NORMALIZED_KEY], origin)
        # direct call on an already normalized asset is a no-op as well
        self.assertEqual(self.qc.normalize_final_asset(asset).storage_key, normalized_key)

    def test_already_conforming_asset_is_left_untouched(self):
        content = make_image()  # 512x512 RGBA PNG: what the fake providers emit
        order, asset = self.single_order_with(content)
        self.qc.start_qc(order=order)
        asset.refresh_from_db()
        self.assertNotIn(NORMALIZED_KEY, asset.metadata or {})
        self.assertEqual(asset.size_bytes, len(content))

    def test_undecodable_asset_is_left_for_the_decodable_check(self):
        order, asset = self.single_order_with(b"junk")
        report = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        self.assertNotIn(NORMALIZED_KEY, asset.metadata or {})
        self.assertFalse(report.automated_checks["wow"]["decodable"])

    def test_normalization_failure_creates_no_report(self):
        order, asset = self.single_order_with(make_image(size=(1024, 1024), mode="RGBA"))
        with mock.patch.object(QcService, "_encode_sticker", side_effect=QcError("boom")):
            with self.assertRaises(QcError):
                self.qc.start_qc(order=order)
        self.assertFalse(QcReport.objects.filter(order=order).exists())
        asset.refresh_from_db()
        self.assertNotIn(NORMALIZED_KEY, asset.metadata or {})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

    def test_flag_off_keeps_current_behaviour(self):
        with mock.patch.dict(os.environ, {NORMALIZE_ENV: "0"}):
            self.assertFalse(normalization_enabled())
            order, asset = self.single_order_with(make_image(size=(1024, 1024), mode="RGBA"))
            original_key = asset.storage_key
            report = self.qc.start_qc(order=order)
        asset.refresh_from_db()
        self.assertEqual(asset.storage_key, original_key)
        self.assertNotIn(NORMALIZED_KEY, asset.metadata or {})
        self.assertFalse(report.automated_checks["wow"]["dimensions"])

    def test_flag_values(self):
        for value, expected in (("1", True), ("", True), ("true", True), ("0", False), ("false", False), ("off", False)):
            with mock.patch.dict(os.environ, {NORMALIZE_ENV: value}):
                self.assertEqual(normalization_enabled(), expected, value)


class NormalizationGateAndDeliveryTests(QcNormalizeTestCase):
    def test_gate_and_delivery_use_the_normalized_file_of_the_same_asset(self):
        order = self.make_order(TRIMMED_PACK_CONFIG)
        assets = {}
        for attempt, slot in enumerate(("e0", "e1", "e2"), start=1):
            assets[slot] = self.add_final_asset(
                self.storage, order, attempt, slot, content=make_image(size=(1024, 1024), mode="RGBA")
            )
        ids_before = self.qc.current_asset_ids(order)
        originals = {slot: a.storage_key for slot, a in assets.items()}

        report = self.qc.start_qc(order=order)
        self.assertEqual(report.asset_ids, ids_before)
        passed = self.qc.finalize_report(report=report, checklist=all_pass_checklist())
        self.assertEqual(passed.status, QcReport.Status.PASSED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
        # gate compares slot_keys + asset_ids: unchanged by normalization
        self.assertEqual(self.qc.assert_delivery_allowed(order=order).pk, passed.pk)

        adapter = FakeAdapter()
        service = FinalDeliveryService(adapter=adapter, storage=self.storage)
        delivery_set = service.delivery_set(order)
        self.assertEqual([a.pk for _, a in delivery_set], ids_before)
        for slot, asset in delivery_set:
            self.assertNotEqual(asset.storage_key, originals[slot])
            self.assertEqual(asset.metadata[NORMALIZED_KEY]["storage_key"], originals[slot])
        plan = service.deliver(order=order, max_items=None)
        self.assertTrue(plan.complete)
        # the customer receives the 512 px files, not the 1024 px originals
        for item in adapter.items:
            image = Image.open(BytesIO(item["content"]))
            self.assertEqual(image.size, (512, 512))
            self.assertEqual(item["mime_type"], "image/png")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)
