from __future__ import annotations

from io import BytesIO

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from PIL import Image

from apps.core.models import GeneratedAsset, Order, QcReport
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
    def __init__(self, *, storage=None):
        self.storage = storage or LocalMediaStorage()

    # --- Domain contract shared with DRF-2051 (full generation) ---------
    # Final assets are GeneratedAsset(kind=FINAL); slot/emotion identity is
    # metadata["emotion"]; an asset replaced by a selective retry is marked
    # metadata["superseded"] = True so re-entry never duplicates slots.

    @staticmethod
    def expected_asset_count(order: Order) -> int:
        """Single expects 1, pack expects 9 — from the product config contract."""
        count = product_emotion_count(order.product)
        if count:
            return count
        return len(order_emotion_codes(order)) or 1

    @staticmethod
    def current_final_assets(order: Order) -> list[GeneratedAsset]:
        return [
            asset
            for asset in order.generated_assets.filter(
                kind=GeneratedAsset.Kind.FINAL,
            ).order_by("created_at", "pk")
            if not (asset.metadata or {}).get("superseded")
        ]

    # --- Automated objective checks --------------------------------------

    def run_automated_checks(self, *, order: Order) -> dict:
        assets = self.current_final_assets(order)
        checks = {
            "expected_count": len(assets) == self.expected_asset_count(order),
        }
        for asset in assets:
            checks[str(asset.pk)] = self._check_asset(asset)
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
    def submit_for_qc(self, *, order: Order) -> QcReport:
        """Enter QUALITY_CONTROL and open a new QC attempt.

        Idempotent: an order already in QC with an open report returns it.
        """
        locked = Order.objects.select_for_update().select_related("product").get(pk=order.pk)
        if locked.status == Order.Status.QUALITY_CONTROL:
            existing = (
                QcReport.objects.filter(order=locked, status=QcReport.Status.IN_PROGRESS)
                .order_by("-attempt")
                .first()
            )
            if existing:
                return existing
        if locked.status != Order.Status.PACK_GENERATING:
            raise QcError(
                f"Order #{locked.pk} cannot enter QC from {locked.status}"
            )
        assets = self.current_final_assets(locked)
        attempt = (
            QcReport.objects.filter(order=locked).aggregate(max_attempt=Max("attempt"))[
                "max_attempt"
            ]
            or 0
        ) + 1
        report = QcReport.objects.create(
            order=locked,
            attempt=attempt,
            expected_count=self.expected_asset_count(locked),
            asset_ids=[asset.pk for asset in assets],
            automated_checks=self.run_automated_checks(order=locked),
        )
        try:
            OrderStateService.transition(
                order=locked,
                to_status=Order.Status.QUALITY_CONTROL,
            )
        except InvalidOrderTransition as exc:
            raise QcError(str(exc)) from exc
        return report

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
            raise QcError(
                f"Order #{order.pk} is not in QC: {order.status}"
            )

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
        for check_name, passed in locked.automated_checks.items():
            if check_name == "expected_count":
                if not passed:
                    reasons.add(CHECK_REASON_CODES[check_name])
                continue
            for asset_check, ok in (passed or {}).items():
                if not ok:
                    reasons.add(CHECK_REASON_CODES[asset_check])
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
    def request_retry(self, *, report: QcReport, asset_ids: list[int]) -> QcReport:
        """Select concrete defective assets for selective retry.

        Only the selected slots are queued; other assets stay untouched.
        The order returns to PACK_GENERATING so the DRF-2051 production
        pipeline can regenerate exactly these slots.
        """
        locked = QcReport.objects.select_for_update().select_related("order").get(pk=report.pk)
        if locked.status != QcReport.Status.FAILED:
            raise QcError("Retry can be requested only for a FAILED QC report")
        order = Order.objects.select_for_update().get(pk=locked.order_id)
        if order.status != Order.Status.QUALITY_CONTROL:
            raise QcError(
                f"Order #{order.pk} is not in QC: {order.status}"
            )
        current = {asset.pk: asset for asset in self.current_final_assets(order)}
        selected = []
        for asset_id in asset_ids:
            asset = current.get(asset_id)
            if asset is None:
                raise QcError(f"Asset #{asset_id} is not a current final asset")
            checks = (locked.automated_checks or {}).get(str(asset.pk)) or {}
            selected.append(
                {
                    "asset_id": asset.pk,
                    "emotion": str((asset.metadata or {}).get("emotion") or ""),
                    "reason_codes": sorted(
                        CHECK_REASON_CODES[name]
                        for name, ok in checks.items()
                        if not ok
                    ),
                }
            )
        if not selected:
            raise QcError("No assets selected for retry")
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

    def assert_delivery_allowed(self, *, order: Order) -> QcReport:
        """Raise QcError unless QC PASS covers the current final asset set."""
        if order.status != Order.Status.READY_FOR_DELIVERY:
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
        current_ids = [asset.pk for asset in self.current_final_assets(order)]
        if current_ids != list(report.asset_ids or []):
            raise QcError(
                "Final assets changed after QC PASS; QC must be repeated"
            )
        return report
