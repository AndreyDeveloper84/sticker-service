from __future__ import annotations

from io import BytesIO

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
        assets = self.current_final_assets(locked)
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
