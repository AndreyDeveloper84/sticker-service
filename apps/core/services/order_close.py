"""«Закрыть заказ» — operator close-out of a live order (DRF-2167 follow-up).

Legacy / abandoned orders must leave the pilot metrics (a PAID order that
never went anywhere counts as «оплаченный без логов» forever) without
touching a single payment, job or asset. The close is:

- superuser-only (enforced by the console), with a mandatory reason and an
  explicit note on the money («возврат сделан / не требуется / нужен»);
- refused while an attempt is PENDING / RUNNING — the operator first takes
  it out of the queue («Снять из очереди») or lets the stale guard reap it;
- CANCELLED when no attempt ever reached the provider, FAILED when a
  billable (or possibly billable) generation exists — the money spent stays
  visible as a failed order, never hidden as a cancellation;
- recorded as one OrderEvent (``order.closed``) with the reason, the money
  note, the status change and the evidence (jobs, payments) — the status
  transition itself goes through OrderStateService like every other.
"""

from __future__ import annotations

from django.db import transaction

from apps.core.models import GenerationJob, Order, OrderEvent
from apps.core.services import generation_cost
from apps.core.services.generation import reap_stale
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService

PAYMENT_NOTES = {
    "refunded": "возврат сделан",
    "not_required": "возврат не требуется",
    "needed": "нужен возврат",
}

# OrderEvent.event_type of the close (a plain CharField value, like the
# DRF-2086 budget events: no AlterField migration during the live pilot).
ORDER_CLOSED = "order.closed"


class OrderCloseError(ValueError):
    pass


class OrderCloseService:
    @staticmethod
    def closable(order: Order) -> bool:
        """Statuses the console offers the action for: anything live after
        payment (before payment the customer closes the order in the bot)."""
        return order.status not in {
            Order.Status.DRAFT,
            Order.Status.AWAITING_PHOTOS,
            Order.Status.READY_FOR_CHECKOUT,
            Order.Status.AWAITING_PAYMENT,
            Order.Status.DELIVERED,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        }

    @staticmethod
    def had_billable_generation(jobs) -> bool:
        """An attempt that reached (or may have reached) the provider: any
        started job whose cost evidence does not prove «not billable». A
        legacy job without a snapshot counts (fail closed)."""
        for job in jobs:
            if job.started_at is None:
                continue
            cost = generation_cost.job_cost(job.input_metadata)
            if cost is None or cost.get("billable") is not False:
                return True
        return False

    @classmethod
    @transaction.atomic
    def close(cls, *, order: Order, actor_ref: str, reason: str, payment_note: str) -> Order:
        reason = (reason or "").strip()
        if not reason:
            raise OrderCloseError("Укажите причину закрытия заказа")
        if payment_note not in PAYMENT_NOTES:
            raise OrderCloseError("Укажите, что с оплатой: возврат сделан / не требуется / нужен")
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if not cls.closable(locked):
            raise OrderCloseError(f"Заказ #{locked.pk} нельзя закрыть из статуса «{locked.get_status_display()}»")
        reap_stale(locked)
        jobs = list(locked.generation_jobs.order_by("pk"))
        active = [
            job for job in jobs
            if job.status in (GenerationJob.Status.PENDING, GenerationJob.Status.RUNNING)
        ]
        if active:
            raise OrderCloseError(
                f"Job #{active[0].pk} ещё в очереди / генерируется — сначала «Снять из очереди» "
                "или дождитесь результата"
            )
        billable = cls.had_billable_generation(jobs)
        target = Order.Status.FAILED if billable else Order.Status.CANCELLED
        if target not in OrderStateService.allowed_targets(locked.status):
            target = Order.Status.FAILED
        from_status = locked.status
        OrderEvent.objects.create(
            order=locked,
            event_type=ORDER_CLOSED,
            actor_kind=OrderEvent.Actor.OPERATOR,
            actor_ref=actor_ref,
            from_status=from_status,
            to_status=target,
            payload={
                "reason": reason,
                "payment_note": payment_note,
                "payment_note_text": PAYMENT_NOTES[payment_note],
                "had_billable_generation": billable,
                "jobs": [{"id": job.pk, "task_type": job.task_type, "status": job.status} for job in jobs],
                "payments": [
                    {"id": p.pk, "provider": p.provider, "status": p.status, "amount_minor": p.amount_minor,
                     "currency": p.currency}
                    for p in locked.payments.order_by("pk")
                ],
            },
        )
        try:
            OrderStateService.transition(order=locked, to_status=target)
        except InvalidOrderTransition as exc:  # pragma: no cover — guarded above
            raise OrderCloseError(str(exc)) from exc
        order.status = locked.status
        return locked
