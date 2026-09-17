from __future__ import annotations

import os
from io import BytesIO
from uuid import uuid4

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from PIL import Image

from apps.core.models import GeneratedAsset, GenerationJob, Order, QcReport
from apps.core.services.channel_order_flow import (
    order_emotion_codes,
    product_emotion_count,
)
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.storage import LocalMediaStorage


class QcError(ValueError):
    pass


# Objective messenger technical format checks (automated). Telegram static
# sticker contract: PNG/WebP, one side exactly 512 px, other side <= 512 px,
# alpha channel, <= 512 KB.
ALLOWED_MIME_TYPES = frozenset({"image/png", "image/webp"})
MIME_FORMATS = {"image/png": "PNG", "image/webp": "WEBP"}
STICKER_SIDE = 512
MAX_FILE_BYTES = 512 * 1024

# Format normalization (DRF-2076): the image API cannot return a 512 px side
# (minimum size 1024), so every real FULL asset is fitted into 512x512 before
# the automated checks. Env-gated for rollback without a deploy:
# QC_NORMALIZE_FINAL_ASSETS=0 disables it (default on).
NORMALIZE_ENV = "QC_NORMALIZE_FINAL_ASSETS"
NORMALIZED_KEY = "normalized_from"
NORMALIZATION_VERSION = 1


def normalization_enabled() -> bool:
    return os.getenv(NORMALIZE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}

# Subjective criteria: human QC only — there is no proven reliable
# automation for these in the Pilot.
HUMAN_CRITERIA = (
    "likeness_face",
    "hair_edges",
    "crop",
    "transparent_background",
    "emotion_readability",
    "ai_artifacts",
)

AUTOMATED_CHECKS = (
    "expected_count",
    "file_present",
    "decodable",
    "mime_type",
    "dimensions",
    "alpha_channel",
    "file_size",
)

# Automated check name -> persisted reason code.
CHECK_REASON_CODES = {
    "expected_count": "incomplete_set",
    "file_present": "missing_file",
    "decodable": "undecodable_image",
    "mime_type": "bad_mime_type",
    "dimensions": "bad_dimensions",
    "alpha_channel": "missing_alpha",
    "file_size": "file_too_large",
}


class QcService:
    """QC domain service (DRF-2052).

    Canonical slot identity is GeneratedAsset.slot_key (DRF-2051 production
    contract: slot_key is the emotion code, FINAL assets are produced by
    FULL GenerationJob attempts). metadata may carry auxiliary data but is
    never domain identity. Asset ids appear in reports strictly as
    audit/reference.
    """

    def __init__(self, *, storage=None):
        self.storage = storage or LocalMediaStorage()

    # --- Domain contract --------------------------------------------------

    @staticmethod
    def expected_asset_count(order: Order) -> int:
        """Single expects 1, pack expects 9 — from the product config contract."""
        count = product_emotion_count(order.product)
        if count:
            return count
        return len(order_emotion_codes(order)) or 1

    @staticmethod
    def current_final_assets(order: Order) -> list[GeneratedAsset]:
        """Current production set, one asset per canonical slot_key.

        DRF-2051 rule: the current asset of a slot is the FINAL asset of
        the slot's latest SUCCEEDED FULL attempt. Older FINAL assets of the
        slot are retained for audit only and never count as current; a
        FINAL asset whose job is not the slot's latest SUCCEEDED attempt is
        ignored even if it is newer by created_at.
        """
        latest_job_by_slot: dict[str, GenerationJob] = {}
        for job in GenerationJob.objects.filter(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
            status=GenerationJob.Status.SUCCEEDED,
        ).order_by("slot_key", "attempt"):
            latest_job_by_slot[str(job.slot_key)] = job
        if not latest_job_by_slot:
            return []
        assets_by_job = {
            asset.job_id: asset
            for asset in order.generated_assets.filter(
                kind=GeneratedAsset.Kind.FINAL,
                job_id__in=[job.pk for job in latest_job_by_slot.values()],
            )
        }
        return [
            assets_by_job[job.pk]
            for job in latest_job_by_slot.values()
            if job.pk in assets_by_job
        ]

    @staticmethod
    def current_slot_keys(order: Order) -> list[str]:
        return [str(asset.slot_key) for asset in QcService.current_final_assets(order)]

    @staticmethod
    def current_asset_ids(order: Order) -> list[int]:
        return [asset.pk for asset in QcService.current_final_assets(order)]

    # --- QC retry bookkeeping (DRF-2079) ----------------------------------

    @staticmethod
    def latest_report(order: Order):
        return QcReport.objects.filter(order=order).order_by("-attempt").first()

    def pending_retry_slots(self, order: Order) -> list[str]:
        """slot_keys the operator sent to retry that still show the SAME
        current asset the FAILED report evaluated — i.e. nothing has been
        regenerated for them yet. Empty once a slot gets a new current asset
        or when the latest report is not a FAILED one with retry_slots."""
        report = self.latest_report(order)
        if report is None or report.status != QcReport.Status.FAILED:
            return []
        current = {str(asset.slot_key): asset.pk for asset in self.current_final_assets(order)}
        pending = []
        for item in report.retry_slots or []:
            slot_key = str(item.get("slot_key") or "")
            if slot_key and current.get(slot_key) == item.get("asset_id"):
                pending.append(slot_key)
        return pending

    def assert_set_changed_since_fail(self, order: Order) -> None:
        """Refuse a new QC attempt on exactly the set the last FAILED report
        sent to retry: a report on unchanged assets can only fail again
        (live evidence DRF-2079: 3 reports on one asset, no new FULL job)."""
        report = self.latest_report(order)
        if (
            report is not None
            and report.status == QcReport.Status.FAILED
            and (report.retry_slots or [])
            and self.current_asset_ids(order) == list(report.asset_ids or [])
        ):
            slots = ", ".join(str(item.get("slot_key")) for item in report.retry_slots)
            raise QcError(
                f"Nothing was regenerated since QC attempt {report.attempt} FAIL; "
                f"regenerate slots {slots} before opening a new report"
            )

    # --- Automated objective checks --------------------------------------

    def run_automated_checks(self, *, order: Order) -> dict:
        assets = self.current_final_assets(order)
        checks = {
            "expected_count": len(assets) == self.expected_asset_count(order),
        }
        for asset in assets:
            checks[str(asset.slot_key)] = {
                "asset_id": asset.pk,  # audit/reference only
                **self._check_asset(asset),
            }
        return checks

    def _check_asset(self, asset: GeneratedAsset) -> dict:
        result = {name: False for name in AUTOMATED_CHECKS if name != "expected_count"}
        if not self.storage.exists(asset.storage_key):
            return result
        result["file_present"] = True
        with self.storage.open(asset.storage_key, "rb") as source:
            content = source.read()
        result["file_size"] = len(content) <= MAX_FILE_BYTES
        try:
            with Image.open(BytesIO(content)) as image:
                result["decodable"] = True
                expected_format = MIME_FORMATS.get(asset.mime_type or "")
                result["mime_type"] = (
                    asset.mime_type in ALLOWED_MIME_TYPES
                    and image.format == expected_format
                )
                width, height = image.size
                result["dimensions"] = (
                    width <= STICKER_SIDE
                    and height <= STICKER_SIDE
                    and (width == STICKER_SIDE or height == STICKER_SIDE)
                )
                result["alpha_channel"] = image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                )
        except Exception:
            result["decodable"] = False
        return result

    # --- Format normalization (DRF-2076) ---------------------------------

    def normalize_final_asset(self, asset: GeneratedAsset) -> GeneratedAsset:
        """Fit the current FINAL asset into the sticker box, in place.

        The asset row (pk, job, slot_key, kind) is unchanged so the QC gate
        and delivery keep working on the same identity; only storage_key,
        mime_type and size_bytes move to the normalized file. The provider
        original stays under its old storage_key and is referenced from
        metadata["normalized_from"] for audit. Idempotent: an asset that
        already carries normalized_from is returned as-is.

        Rules: longest side becomes exactly 512 px, aspect preserved, never
        upscaled beyond the source; alpha is preserved but NEVER invented —
        an opaque source stays opaque so alpha_channel fails honestly; PNG
        first, WebP (lossless, then quality 90) only when PNG exceeds the
        512 KB limit. An undecodable source is left untouched for the
        automated "decodable" check to report.
        """
        if (asset.metadata or {}).get(NORMALIZED_KEY):
            return asset
        if not self.storage.exists(asset.storage_key):
            return asset
        with self.storage.open(asset.storage_key, "rb") as source:
            original = source.read()
        try:
            with Image.open(BytesIO(original)) as image:
                image.load()
                source_info = {
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "format": image.format or "",
                }
                if self._already_sticker_sized(image, len(original), asset.mime_type):
                    return asset  # within contract already: nothing to rewrite
                normalized = self._fit_sticker(image)
        except QcError:
            raise
        except Exception:
            return asset  # undecodable: automated checks record decodable=False

        content, mime_type, extension = self._encode_sticker(normalized)
        base = asset.storage_key.rsplit(".", 1)[0]
        new_key = f"{base}-normalized-{uuid4().hex[:8]}.{extension}"
        self.storage.save(new_key, BytesIO(content))

        metadata = dict(asset.metadata or {})
        metadata[NORMALIZED_KEY] = {
            "storage_key": asset.storage_key,
            "mime_type": asset.mime_type or "",
            "size_bytes": len(original),
            **source_info,
            "version": NORMALIZATION_VERSION,
            "at": timezone.now().isoformat(),
        }
        asset.storage_key = new_key
        asset.mime_type = mime_type
        asset.size_bytes = len(content)
        asset.metadata = metadata
        asset.save(update_fields=["storage_key", "mime_type", "size_bytes", "metadata", "updated_at"])
        return asset

    @staticmethod
    def _already_sticker_sized(image: Image.Image, size_bytes: int, mime_type: str) -> bool:
        width, height = image.size
        return (
            width <= STICKER_SIDE
            and height <= STICKER_SIDE
            and (width == STICKER_SIDE or height == STICKER_SIDE)
            and size_bytes <= MAX_FILE_BYTES
            and mime_type in ALLOWED_MIME_TYPES
            and image.format == MIME_FORMATS.get(mime_type)
        )

    @staticmethod
    def _fit_sticker(image: Image.Image) -> Image.Image:
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        # Keep alpha when the source has it; never add one to an opaque image.
        converted = image.convert("RGBA" if has_alpha else "RGB")
        width, height = converted.size
        longest = max(width, height)
        if longest <= 0:
            raise QcError("Final asset has no pixels")
        scale = STICKER_SIDE / longest
        target = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        # Exactly one side must be 512 after rounding.
        if width >= height:
            target = (STICKER_SIDE, target[1])
        else:
            target = (target[0], STICKER_SIDE)
        if target == converted.size:
            return converted
        return converted.resize(target, Image.Resampling.LANCZOS)

    @staticmethod
    def _encode_sticker(image: Image.Image) -> tuple[bytes, str, str]:
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        content = buffer.getvalue()
        if len(content) <= MAX_FILE_BYTES:
            return content, "image/png", "png"
        for kwargs in ({"lossless": True}, {"quality": 90, "method": 6}):
            buffer = BytesIO()
            image.save(buffer, format="WEBP", **kwargs)
            content = buffer.getvalue()
            if len(content) <= MAX_FILE_BYTES:
                return content, "image/webp", "webp"
        raise QcError(
            f"Normalized sticker still exceeds {MAX_FILE_BYTES} bytes ({len(content)})"
        )

    # --- QC lifecycle -----------------------------------------------------

    @transaction.atomic
    def start_qc(self, *, order: Order) -> QcReport:
        """Open a QC attempt for an order that DRF-2051 put into QC.

        Entry into QUALITY_CONTROL (production completion) is owned by
        DRF-2051; this service only opens reports on orders already there.
        Idempotent: an open report is returned as-is.
        """
        locked = Order.objects.select_for_update().select_related("product").get(pk=order.pk)
        if locked.status != Order.Status.QUALITY_CONTROL:
            raise QcError(
                f"Order #{locked.pk} is not in quality control: {locked.status}"
            )
        existing = (
            QcReport.objects.filter(order=locked, status=QcReport.Status.IN_PROGRESS)
            .order_by("-attempt")
            .first()
        )
        if existing:
            return existing
        self.assert_set_changed_since_fail(locked)
        assets = self.current_final_assets(locked)
        if normalization_enabled():
            # Same transaction as the report: a normalization failure raises
            # QcError and no QcReport row is created.
            assets = [self.normalize_final_asset(asset) for asset in assets]
        attempt = (
            QcReport.objects.filter(order=locked).aggregate(max_attempt=Max("attempt"))[
                "max_attempt"
            ]
            or 0
        ) + 1
        return QcReport.objects.create(
            order=locked,
            attempt=attempt,
            expected_count=self.expected_asset_count(locked),
            slot_keys=[str(asset.slot_key) for asset in assets],
            asset_ids=[asset.pk for asset in assets],
            automated_checks=self.run_automated_checks(order=locked),
        )

    @transaction.atomic
    def finalize_report(self, *, report: QcReport, checklist: dict) -> QcReport:
        """Close a QC attempt as PASS or FAIL.

        PASS requires the full expected asset set, all automated checks and
        every human criterion. Repeated finalize on a completed report is a
        safe no-op. FAIL keeps the order in QUALITY_CONTROL and persists
        reason codes.
        """
        locked = QcReport.objects.select_for_update().select_related("order").get(pk=report.pk)
        if locked.status != QcReport.Status.IN_PROGRESS:
            return locked
        order = Order.objects.select_for_update().get(pk=locked.order_id)
        if order.status != Order.Status.QUALITY_CONTROL:
            raise QcError(f"Order #{order.pk} is not in quality control: {order.status}")

        unknown = set(checklist) - set(HUMAN_CRITERIA)
        if unknown:
            raise QcError(f"Unknown checklist criteria: {sorted(unknown)}")
        missing = [name for name in HUMAN_CRITERIA if name not in checklist]
        if missing:
            raise QcError(f"Checklist is incomplete: {missing}")
        locked.human_checklist = {
            name: {"passed": bool(checklist[name])} for name in HUMAN_CRITERIA
        }

        reasons = set()
        for check_key, checks in (locked.automated_checks or {}).items():
            if check_key == "expected_count":
                if not checks:
                    reasons.add(CHECK_REASON_CODES["expected_count"])
                continue
            for check_name, ok in (checks or {}).items():
                if check_name == "asset_id":
                    continue
                if not ok:
                    reasons.add(CHECK_REASON_CODES[check_name])
        for criterion, item in locked.human_checklist.items():
            if not item["passed"]:
                reasons.add(criterion)
        locked.reason_codes = sorted(reasons)
        locked.completed_at = timezone.now()

        if reasons:
            locked.status = QcReport.Status.FAILED
            locked.save()
            return locked

        locked.status = QcReport.Status.PASSED
        locked.retry_slots = []
        locked.save()
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.READY_FOR_DELIVERY,
            )
        except InvalidOrderTransition as exc:
            raise QcError(str(exc)) from exc
        return locked

    @transaction.atomic
    def request_retry(self, *, report: QcReport, slot_keys: list[str]) -> QcReport:
        """Fix the canonical slot_key set that DRF-2051 must regenerate.

        Only the selected slots are queued; other assets stay untouched.
        asset_id is recorded for audit only — slot_key is the canonical
        retry identity. The order returns to PACK_GENERATING; the operator
        then runs FullProductionService.regenerate_slots(slot_keys=...)
        (DRF-2051), which re-enters QUALITY_CONTROL only once the latest
        attempt of every slot has succeeded.
        """
        locked = QcReport.objects.select_for_update().select_related("order").get(pk=report.pk)
        if locked.status != QcReport.Status.FAILED:
            raise QcError("Retry can be requested only for a FAILED QC report")
        order = Order.objects.select_for_update().get(pk=locked.order_id)
        if order.status != Order.Status.QUALITY_CONTROL:
            raise QcError(f"Order #{order.pk} is not in quality control: {order.status}")
        current = {
            str(asset.slot_key): asset for asset in self.current_final_assets(order)
        }
        selected = []
        for slot_key in slot_keys:
            asset = current.get(str(slot_key))
            if asset is None:
                raise QcError(f"Slot {slot_key!r} is not a current final asset slot")
            checks = (locked.automated_checks or {}).get(str(slot_key)) or {}
            selected.append(
                {
                    "slot_key": str(slot_key),
                    "asset_id": asset.pk,  # audit/reference only
                    "reason_codes": sorted(
                        CHECK_REASON_CODES[name]
                        for name, ok in checks.items()
                        if name != "asset_id" and not ok
                    ),
                }
            )
        if not selected:
            raise QcError("No slots selected for retry")
        locked.retry_slots = selected
        locked.save(update_fields=["retry_slots", "updated_at"])
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PACK_GENERATING,
            )
        except InvalidOrderTransition as exc:
            raise QcError(str(exc)) from exc
        return locked

    # --- Delivery gate (consumed by DRF-2053) ------------------------------

    # Statuses in which sending final assets is legal at all: the QC PASS
    # exit and the delivery run DRF-2053 enters from it (resume re-checks
    # the gate before every send, so a set changed mid-delivery is caught).
    DELIVERY_STATUSES = frozenset(
        {Order.Status.READY_FOR_DELIVERY, Order.Status.DELIVERY_IN_PROGRESS}
    )

    def assert_delivery_allowed(self, *, order: Order) -> QcReport:
        """Raise QcError unless QC PASS covers the current final asset set.

        The set is compared by slot_key AND by the concrete assets that
        were evaluated: a slot regenerated after PASS keeps its slot_key
        but gets a new current asset, which must go through QC again.
        """
        if order.status not in self.DELIVERY_STATUSES:
            raise QcError(
                f"Delivery is forbidden before QC PASS (order #{order.pk}: {order.status})"
            )
        report = (
            QcReport.objects.filter(order=order, status=QcReport.Status.PASSED)
            .order_by("-attempt")
            .first()
        )
        if report is None:
            raise QcError(f"Order #{order.pk} has no passed QC report")
        if self.current_slot_keys(order) != list(report.slot_keys or []) or (
            self.current_asset_ids(order) != list(report.asset_ids or [])
        ):
            raise QcError("Final assets changed after QC PASS; QC must be repeated")
        return report
