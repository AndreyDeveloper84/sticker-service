"""Final sticker set delivery to the order channel (DRF-2053).

Delivers the CURRENT final asset of every expected production slot to the
customer through the channel the order was placed in (Telegram / MAX).

Delivery contract
-----------------
- Entry: READY_FOR_DELIVERY (QC PASS, DRF-2052) -> DELIVERY_IN_PROGRESS.
  Re-entry (resume) from DELIVERY_IN_PROGRESS sends only what is still
  unsent. DELIVERED is reached only when every expected slot has a
  successful send and the final "set is ready" message went out.
- QC gate: QcService.assert_delivery_allowed() must pass before ANY send,
  on entry and on every resume. It rejects an order without QC PASS and a
  PASS that no longer covers the current asset set (slot regenerated
  after PASS). No channel adapter is reachable around this gate.
- Set: expected slots = Order.selection["emotions"] (send order), count
  must equal Product.config["quantity"]; the slot asset is the current
  FINAL asset under the DRF-2051 production contract (latest SUCCEEDED
  FULL attempt), and the slot's latest attempt must itself be SUCCEEDED.
  An incomplete or unsettled set is an error and nothing is sent.
- Audit: every run is a FinalDelivery row (order, attempt) with per-slot
  results. Idempotency is by slot_key across ALL runs of the order: a slot
  with a "sent" result (message_id) is never sent again.
- Failures: each slot records its own outcome; one slot's failure does not
  abort the others. The order stays DELIVERY_IN_PROGRESS; re-sending is an
  explicit operator action (resume), never an automatic loop.
- Known Pilot limitation (crash window): if the channel accepted a message
  but the process died before the message_id was persisted, the slot has
  no "sent" record and the operator's resume sends it once more. One such
  duplicate is accepted for the Pilot; persisted sends are never repeated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.models import FinalDelivery, GeneratedAsset, GenerationJob, Order
from apps.core.services.channel_order_flow import (
    order_emotion_codes,
    product_emotion_options,
)
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.services.preview_delivery import DeliveryResult
from apps.core.services.qc import QcError, QcService
from apps.core.storage import LocalMediaStorage

SUMMARY_TEXT = (
    "Ваш набор стикеров готов! Все стикеры выше — сохраните их себе. "
    "Спасибо за заказ."
)


class FinalDeliveryError(ValueError):
    pass


class FinalDeliveryAdapter(Protocol):
    """Channel transport for the final set; implementations live in
    apps.telegram_bot / apps.max_bot next to the preview adapters."""

    channel: str

    def send_final_item(
        self,
        *,
        recipient_id: str,
        content: bytes,
        mime_type: str,
        filename: str,
        caption: str,
        index: int,
        total: int,
    ) -> DeliveryResult: ...

    def send_final_summary(
        self,
        *,
        recipient_id: str,
        text: str,
        total: int,
    ) -> DeliveryResult: ...


def classify_delivery_failure(exc: Exception) -> str:
    """"retryable" (network / 5xx / 429) or "permanent" (other 4xx).

    Both TelegramAPIError and MaxAPIError expose ``status_code`` with 0 for
    network-level failures. Unknown exceptions are treated as retryable:
    re-sending a file cannot duplicate billable work, and the retry is an
    explicit operator action anyway.
    """
    if getattr(exc, "retryable", None) is True:
        # e.g. MaxAttachmentNotReady: 400, but nothing was sent — resend later
        return "retryable"
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or status <= 0:
        return "retryable"
    if status == 429 or status >= 500:
        return "retryable"
    return "permanent"


@dataclass(frozen=True)
class SlotDeliveryState:
    """Per-slot delivery overview across all runs of the order."""

    slot_key: str
    asset_id: int | None
    status: str  # "pending" | "sent" | "failed"
    message_id: str
    error: str
    failure_class: str  # "" | "retryable" | "permanent"
    attempt: int | None  # run that produced the recorded outcome


@dataclass(frozen=True)
class DeliveryPlan:
    slots: list[SlotDeliveryState]
    summary_status: str  # "pending" | "sent" | "failed"
    attempts: int

    @property
    def complete(self) -> bool:
        return all(slot.status == "sent" for slot in self.slots) and (
            self.summary_status == "sent"
        )


class FinalDeliveryService:
    """Send the final set; safe to re-run (no duplicate sends).

    ``max_items`` bounds how many slot sends one call performs (None = no
    limit) so a synchronous console request stays within the worker
    timeout; re-run until the plan is complete.
    """

    def __init__(
        self, *, adapter: FinalDeliveryAdapter, storage=None, qc: QcService | None = None
    ):
        self.adapter = adapter
        self.storage = storage or LocalMediaStorage()
        self.qc = qc or QcService(storage=self.storage)

    # ------------------------------------------------------------- set

    @staticmethod
    def expected_slots(order: Order) -> list[str]:
        config = order.product.config or {}
        try:
            quantity = int(config.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            raise FinalDeliveryError(
                f"Product {order.product.code} has no production quantity"
            )
        codes = order_emotion_codes(order)
        if len(codes) != quantity or len(set(codes)) != len(codes):
            raise FinalDeliveryError(
                f"Order #{order.pk} emotion selection does not match "
                f"product quantity {quantity}"
            )
        return codes

    @staticmethod
    def current_final_assets(order: Order) -> dict[str, GeneratedAsset]:
        """slot_key -> current FINAL asset under the DRF-2051 contract.

        The rule (FINAL asset of the slot's latest SUCCEEDED FULL attempt)
        is not redefined here: the same QcService view that the delivery
        gate evaluated is used, so delivery sends exactly the QC-passed set.
        """
        return {str(asset.slot_key): asset for asset in QcService.current_final_assets(order)}

    @staticmethod
    def unsettled_slots(order: Order) -> list[str]:
        """Slots whose LATEST FULL attempt is not SUCCEEDED.

        A slot regenerated after its last success and still running, or
        failed / ambiguous, keeps an older current asset in the DRF-2051
        plan; delivery must not ship that stale asset.
        """
        latest_status: dict[str, str] = {}
        for job in GenerationJob.objects.filter(
            order=order, task_type=GenerationJob.TaskType.FULL
        ).order_by("slot_key", "attempt"):
            latest_status[str(job.slot_key)] = job.status
        return sorted(
            slot
            for slot, status in latest_status.items()
            if status != GenerationJob.Status.SUCCEEDED
        )

    def delivery_set(self, order: Order) -> list[tuple[str, GeneratedAsset]]:
        """Ordered (slot_key, asset) pairs; raises if the set is not deliverable."""
        slots = self.expected_slots(order)
        current = self.current_final_assets(order)
        missing = [slot for slot in slots if slot not in current]
        if missing:
            raise FinalDeliveryError(
                f"Order #{order.pk} final set is incomplete, missing slots: "
                f"{', '.join(missing)}"
            )
        if len(current) != len(slots):
            raise FinalDeliveryError(
                f"Order #{order.pk} has final assets outside the expected set"
            )
        unsettled = [slot for slot in self.unsettled_slots(order) if slot in current]
        if unsettled:
            raise FinalDeliveryError(
                f"Order #{order.pk} latest FULL attempt is not succeeded for slots: "
                f"{', '.join(unsettled)}"
            )
        return [(slot, current[slot]) for slot in slots]

    # ------------------------------------------------------------ plan

    @staticmethod
    def _runs(order: Order) -> list[FinalDelivery]:
        return list(order.final_deliveries.order_by("attempt"))

    @staticmethod
    def _outcomes(runs) -> tuple[dict[str, dict], dict]:
        """Latest per-slot outcome across runs (sent wins) and summary state."""
        latest: dict[str, dict] = {}
        summary: dict = {}
        for run in runs:
            for item in run.results or []:
                slot = str(item.get("slot_key") or "")
                if not slot:
                    continue
                if latest.get(slot, {}).get("status") == "sent":
                    continue  # a sent slot is final; later failures cannot undo it
                latest[slot] = {**item, "attempt": run.attempt}
            if run.summary and summary.get("status") != "sent":
                summary = {**run.summary, "attempt": run.attempt}
        return latest, summary

    def delivery_plan(self, order: Order) -> DeliveryPlan:
        slots = self.expected_slots(order)
        current = self.current_final_assets(order)
        runs = self._runs(order)
        outcomes, summary = self._outcomes(runs)
        states = []
        for slot in slots:
            asset = current.get(slot)
            outcome = outcomes.get(slot)
            if outcome is None:
                states.append(
                    SlotDeliveryState(
                        slot, asset.pk if asset else None, "pending", "", "", "", None
                    )
                )
                continue
            states.append(
                SlotDeliveryState(
                    slot_key=slot,
                    asset_id=outcome.get("asset_id") or (asset.pk if asset else None),
                    status=str(outcome.get("status") or "pending"),
                    message_id=str(outcome.get("message_id") or ""),
                    error=str(outcome.get("error") or ""),
                    failure_class=str(outcome.get("failure_class") or ""),
                    attempt=outcome.get("attempt"),
                )
            )
        return DeliveryPlan(
            slots=states,
            summary_status=str(summary.get("status") or "pending"),
            attempts=len(runs),
        )

    # --------------------------------------------------------- entries

    def deliver(self, *, order: Order, max_items: int | None = None) -> DeliveryPlan:
        """Start (READY_FOR_DELIVERY) or resume (DELIVERY_IN_PROGRESS) delivery."""
        run, items = self._prepare(order=order, allow_entry=True)
        return self._run(order=order, run=run, items=items, max_items=max_items)

    def resume(self, *, order: Order, max_items: int | None = None) -> DeliveryPlan:
        """Resume only: sends the slots that are still unsent, never duplicates."""
        run, items = self._prepare(order=order, allow_entry=False)
        return self._run(order=order, run=run, items=items, max_items=max_items)

    # ------------------------------------------------------- internals

    def _prepare(self, *, order: Order, allow_entry: bool):
        if order.channel_identity.channel != self.adapter.channel:
            raise FinalDeliveryError("Delivery adapter does not match order channel")
        with transaction.atomic():
            locked = (
                Order.objects.select_for_update()
                .select_related("product", "channel_identity")
                .get(pk=order.pk)
            )
            if locked.status == Order.Status.READY_FOR_DELIVERY:
                if not allow_entry:
                    raise FinalDeliveryError(
                        f"Order #{locked.pk} delivery has not started yet; use deliver()"
                    )
            elif locked.status != Order.Status.DELIVERY_IN_PROGRESS:
                raise FinalDeliveryError(
                    f"Order #{locked.pk} cannot deliver final set from {locked.status}"
                )
            # QC delivery gate (DRF-2052): PASS must cover the CURRENT set.
            # Evaluated under the order lock on entry AND on every resume,
            # before a run row exists and before any channel call.
            try:
                self.qc.assert_delivery_allowed(order=locked)
            except QcError as exc:
                raise FinalDeliveryError(str(exc)) from exc
            items = self.delivery_set(locked)

            latest = locked.final_deliveries.order_by("attempt").last()
            if latest is not None and latest.status == FinalDelivery.Status.IN_PROGRESS:
                run = latest
            else:
                attempt = (
                    locked.final_deliveries.aggregate(max_attempt=Max("attempt"))[
                        "max_attempt"
                    ]
                    or 0
                ) + 1
                run = FinalDelivery.objects.create(
                    order=locked,
                    channel=self.adapter.channel,
                    attempt=attempt,
                    status=FinalDelivery.Status.IN_PROGRESS,
                    started_at=timezone.now(),
                )
            if locked.status == Order.Status.READY_FOR_DELIVERY:
                try:
                    OrderStateService.transition(
                        order=locked, to_status=Order.Status.DELIVERY_IN_PROGRESS
                    )
                except InvalidOrderTransition as exc:
                    raise FinalDeliveryError(str(exc)) from exc
            order.status = locked.status
            return run, items

    def _run(self, *, order: Order, run: FinalDelivery, items, max_items):
        recipient = order.channel_identity.external_user_id
        labels = {
            option["code"]: option["label"]
            for option in product_emotion_options(order.product)
        }
        outcomes, summary = self._outcomes(self._runs(order))
        total = len(items)
        sent_count = 0
        failed = False
        for index, (slot, asset) in enumerate(items, start=1):
            if outcomes.get(slot, {}).get("status") == "sent":
                continue  # idempotency: never re-send a delivered slot
            if max_items is not None and sent_count >= max_items:
                break
            sent_count += 1
            if not self._send_slot(
                run=run,
                recipient=recipient,
                slot=slot,
                asset=asset,
                caption=f"Стикер {index}/{total} · {labels.get(slot, slot)}",
                index=index,
                total=total,
            ):
                failed = True

        outcomes, summary = self._outcomes(self._runs(order))
        all_sent = all(outcomes.get(slot, {}).get("status") == "sent" for slot, _ in items)
        if all_sent and summary.get("status") != "sent":
            if not self._send_summary(run=run, recipient=recipient, total=total):
                failed = True
            _outcomes, summary = self._outcomes(self._runs(order))

        return self._finalize(
            order=order,
            run=run,
            complete=all_sent and summary.get("status") == "sent",
            failed=failed,
        )

    def _send_slot(self, *, run, recipient, slot, asset, caption, index, total) -> bool:
        try:
            if not self.storage.exists(asset.storage_key):
                raise FinalDeliveryError(f"Final asset #{asset.pk} file is missing")
            with self.storage.open(asset.storage_key, "rb") as source:
                content = source.read()
            result = self.adapter.send_final_item(
                recipient_id=recipient,
                content=content,
                mime_type=asset.mime_type or "image/png",
                filename=f"sticker-{slot}.png",
                caption=caption,
                index=index,
                total=total,
            )
            if not result.message_id:
                raise FinalDeliveryError("Channel returned no message id")
        except Exception as exc:
            self._record_slot(
                run=run,
                slot=slot,
                asset=asset,
                status="failed",
                message_id="",
                error=str(exc)[:1000],
                failure_class=classify_delivery_failure(exc),
                metadata={},
            )
            return False
        self._record_slot(
            run=run,
            slot=slot,
            asset=asset,
            status="sent",
            message_id=str(result.message_id),
            error="",
            failure_class="",
            metadata=result.metadata or {},
        )
        return True

    def _send_summary(self, *, run, recipient, total) -> bool:
        try:
            result = self.adapter.send_final_summary(
                recipient_id=recipient, text=SUMMARY_TEXT, total=total
            )
        except Exception as exc:
            self._record_summary(
                run=run,
                status="failed",
                message_id="",
                error=str(exc)[:1000],
                failure_class=classify_delivery_failure(exc),
            )
            return False
        self._record_summary(
            run=run,
            status="sent",
            message_id=str(result.message_id),
            error="",
            failure_class="",
        )
        return True

    @transaction.atomic
    def _record_slot(
        self, *, run, slot, asset, status, message_id, error, failure_class, metadata
    ) -> None:
        locked = FinalDelivery.objects.select_for_update().get(pk=run.pk)
        results = list(locked.results or [])
        results.append(
            {
                "slot_key": slot,
                "asset_id": asset.pk,
                "message_id": message_id,
                "status": status,
                "error": error,
                "failure_class": failure_class,
                "metadata": metadata,
                "created_at": timezone.now().isoformat(),
            }
        )
        locked.results = results
        locked.save(update_fields=["results", "updated_at"])
        run.results = results

    @transaction.atomic
    def _record_summary(self, *, run, status, message_id, error, failure_class) -> None:
        locked = FinalDelivery.objects.select_for_update().get(pk=run.pk)
        locked.summary = {
            "status": status,
            "message_id": message_id,
            "error": error,
            "failure_class": failure_class,
            "created_at": timezone.now().isoformat(),
        }
        locked.save(update_fields=["summary", "updated_at"])
        run.summary = locked.summary

    @transaction.atomic
    def _finalize(self, *, order, run, complete, failed) -> DeliveryPlan:
        locked_run = FinalDelivery.objects.select_for_update().get(pk=run.pk)
        locked = Order.objects.select_for_update().select_related("product").get(pk=order.pk)
        if complete:
            locked_run.status = FinalDelivery.Status.SENT
            locked_run.finished_at = timezone.now()
            if locked.status == Order.Status.DELIVERY_IN_PROGRESS:
                try:
                    OrderStateService.transition(
                        order=locked, to_status=Order.Status.DELIVERED
                    )
                except InvalidOrderTransition as exc:
                    raise FinalDeliveryError(str(exc)) from exc
        elif failed:
            locked_run.status = FinalDelivery.Status.FAILED
            locked_run.finished_at = timezone.now()
        # else: max_items limit reached without failure — the run stays
        # IN_PROGRESS and the next deliver()/resume() call continues it.
        locked_run.save(update_fields=["status", "finished_at", "updated_at"])
        order.status = locked.status
        return self.delivery_plan(locked)
