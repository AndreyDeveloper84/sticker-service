"""Pilot metrics snapshot (DRF-2055).

Answers the nine pilot questions from existing tables plus the append-only
OrderEvent log. Everything is computed on demand; there is no aggregation
table and no external analytics.

Stage reach is "ever reached": an order counts for a stage if the status log
shows a transition into it, or (for orders created before the log existed)
if its current status is that stage or a later one on the happy path.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import Count, Sum

from apps.core.models import ChannelIdentity, GenerationJob, Order, OrderEvent, Payment, Revision

# Happy path in order; later stages imply earlier ones for pre-log orders.
FUNNEL_STAGES = [
    Order.Status.AWAITING_PHOTOS,
    Order.Status.READY_FOR_CHECKOUT,
    Order.Status.AWAITING_PAYMENT,
    Order.Status.PAID,
    Order.Status.PREVIEW_GENERATING,
    Order.Status.INTERNAL_PREVIEW_REVIEW,
    Order.Status.PREVIEW_REVIEW,
    Order.Status.PACK_GENERATING,
    Order.Status.QUALITY_CONTROL,
]

# Post-production stages (DRF-2052 QC exit, DRF-2053 delivery). DELIVERY_IN_PROGRESS
# is a transient step between them and is not a funnel stage of its own.
LATER_STAGES = [Order.Status.READY_FOR_DELIVERY, Order.Status.DELIVERED]

TERMINAL_STATUSES = {Order.Status.CANCELLED, Order.Status.FAILED}


def _ratio(numerator: int, denominator: int):
    return round(numerator / denominator, 4) if denominator else None


@dataclass(frozen=True)
class PilotMetricsService:
    since: datetime | None = None
    until: datetime | None = None

    def _orders(self):
        qs = Order.objects.all()
        if self.since:
            qs = qs.filter(created_at__gte=self.since)
        if self.until:
            qs = qs.filter(created_at__lt=self.until)
        return qs

    def snapshot(self) -> dict:
        orders = self._orders()
        order_ids = list(orders.values_list("pk", flat=True))
        events = OrderEvent.objects.filter(order_id__in=order_ids)
        status_now = dict(orders.values_list("pk", "status"))

        reached = self._reached(events, status_now)
        paid_orders = reached.get(Order.Status.PAID, set())
        preview_review_orders = reached.get(Order.Status.PREVIEW_REVIEW, set())

        return {
            "window": {
                "since": self.since.isoformat() if self.since else None,
                "until": self.until.isoformat() if self.until else None,
            },
            "funnel": self._funnel(order_ids, reached, status_now),
            "drop_off": self._drop_off(events, status_now),
            "payments": self._payments(order_ids),
            "previews": self._previews(order_ids),
            "approval": self._approval(events, order_ids, preview_review_orders),
            "generation_cost": self._generation_cost(order_ids, paid_orders),
            "manual_work": self._manual_work(events, paid_orders),
            "delivery": self._delivery(reached, status_now),
        }

    # -- funnel -------------------------------------------------------------

    @staticmethod
    def _reached(events, status_now) -> dict[str, set[int]]:
        reached: dict[str, set[int]] = {}
        for order_id, to_status in (
            events.filter(event_type=OrderEvent.Type.STATUS_CHANGED).values_list("order_id", "to_status")
        ):
            reached.setdefault(to_status, set()).add(order_id)

        # Orders without any status event predate the log: infer from the
        # current status (a later happy-path stage implies the earlier ones).
        logged = set().union(*reached.values()) if reached else set()
        path = FUNNEL_STAGES + LATER_STAGES
        for order_id, status in status_now.items():
            if order_id in logged:
                continue
            if status in path:
                for stage in path[: path.index(status) + 1]:
                    reached.setdefault(stage, set()).add(order_id)
            elif status in TERMINAL_STATUSES:
                reached.setdefault(status, set()).add(order_id)
        return reached

    def _funnel(self, order_ids, reached, status_now) -> dict:
        identities = ChannelIdentity.objects.all()
        if self.since:
            identities = identities.filter(created_at__gte=self.since)
        if self.until:
            identities = identities.filter(created_at__lt=self.until)

        stages = {stage: len(reached.get(stage, set())) for stage in FUNNEL_STAGES + LATER_STAGES}
        return {
            "identities": identities.count(),
            "identities_without_orders": identities.filter(orders__isnull=True).count(),
            "orders_created": len(order_ids),
            "reached": stages,
            "cancelled": len(reached.get(Order.Status.CANCELLED, set())),
            "failed": len(reached.get(Order.Status.FAILED, set())),
            "current_status": dict(Counter(status_now.values())),
        }

    @staticmethod
    def _drop_off(events, status_now) -> dict:
        terminal_from = Counter()
        for from_status, to_status in (
            events.filter(
                event_type=OrderEvent.Type.STATUS_CHANGED,
                to_status__in=list(TERMINAL_STATUSES),
            ).values_list("from_status", "to_status")
        ):
            terminal_from[f"{from_status}->{to_status}"] += 1

        logged_terminal = set(
            events.filter(
                event_type=OrderEvent.Type.STATUS_CHANGED,
                to_status__in=list(TERMINAL_STATUSES),
            ).values_list("order_id", flat=True)
        )
        unknown = sum(
            1 for order_id, status in status_now.items() if status in TERMINAL_STATUSES and order_id not in logged_terminal
        )
        open_by_stage = Counter(status for status in status_now.values() if status not in TERMINAL_STATUSES)
        return {
            "terminal_from_status": dict(terminal_from),
            "terminal_without_log": unknown,
            "open_orders_by_current_status": dict(open_by_stage),
        }

    # -- payments -----------------------------------------------------------

    @staticmethod
    def _payments(order_ids) -> dict:
        confirmed = Payment.objects.filter(order_id__in=order_ids, status=Payment.Status.CONFIRMED)
        by_provider = {}
        for row in confirmed.values("provider", "currency").annotate(count=Count("pk"), total=Sum("amount_minor")).order_by():
            by_provider[f"{row['provider']}/{row['currency']}"] = {"count": row["count"], "amount_minor": row["total"]}
        # DRF-2086: refunds are recorded as payment.refunded events by the
        # (future) refund flow; the KPI "refund rate" (DRF-2049) reads them here.
        refunded = OrderEvent.objects.filter(order_id__in=order_ids, event_type=OrderEvent.PAYMENT_REFUNDED)
        return {
            "orders_paid": confirmed.values("order_id").distinct().count(),
            "confirmed_payments": confirmed.count(),
            "by_provider_currency": by_provider,
            "refunded": refunded.values("order_id").distinct().count(),
        }

    # -- previews / approval ------------------------------------------------

    @staticmethod
    def _previews(order_ids) -> dict:
        jobs = GenerationJob.objects.filter(order_id__in=order_ids)
        preview = jobs.filter(task_type=GenerationJob.TaskType.PREVIEW)
        revision = jobs.filter(task_type=GenerationJob.TaskType.REVISION)
        return {
            "preview_jobs": preview.count(),
            "preview_jobs_succeeded": preview.filter(status=GenerationJob.Status.SUCCEEDED).count(),
            "preview_jobs_failed": preview.filter(status=GenerationJob.Status.FAILED).count(),
            "orders_with_preview": preview.filter(status=GenerationJob.Status.SUCCEEDED).values("order_id").distinct().count(),
            "revision_jobs": revision.count(),
        }

    @staticmethod
    def _approval(events, order_ids, preview_review_orders) -> dict:
        approved = set(
            events.filter(event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED).values_list("order_id", flat=True)
        )
        revised = set(Revision.objects.filter(order_id__in=order_ids).values_list("order_id", flat=True))
        reviewed = len(preview_review_orders)
        return {
            "orders_reached_preview_review": reviewed,
            "orders_customer_approved": len(approved),
            "orders_revision_requested": len(revised),
            "approval_rate": _ratio(len(approved), reviewed),
            "revision_rate": _ratio(len(revised), reviewed),
            "revision_by_category": dict(
                Counter(Revision.objects.filter(order_id__in=order_ids).values_list("category", flat=True))
            ),
        }

    # -- cost ---------------------------------------------------------------

    @staticmethod
    def _generation_cost(order_ids, paid_orders) -> dict:
        # One provider call per job that actually started, regardless of outcome.
        calls = GenerationJob.objects.filter(order_id__in=order_ids, started_at__isnull=False)
        by_task = dict(Counter(calls.values_list("task_type", flat=True)))
        tokens = Counter()
        jobs_with_usage = 0
        for metadata in calls.values_list("output_metadata", flat=True):
            usage = (metadata or {}).get("usage") or {}
            if not usage:
                continue
            jobs_with_usage += 1
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int):
                    tokens[key] += value

        unit_cost = getattr(settings, "PILOT_IMAGE_CALL_COST_USD", None)
        total_calls = calls.count()
        estimated = round(total_calls * float(unit_cost), 4) if unit_cost is not None else None
        paid = len(paid_orders)
        return {
            "provider_calls": total_calls,
            "provider_calls_by_task_type": by_task,
            "jobs_with_usage": jobs_with_usage,
            "tokens": dict(tokens),
            "unit_cost_usd": float(unit_cost) if unit_cost is not None else None,
            "estimated_usd": estimated,
            "calls_per_paid_order": _ratio(total_calls, paid),
            "estimated_usd_per_paid_order": round(estimated / paid, 4) if estimated is not None and paid else None,
        }

    # -- manual work ----------------------------------------------------------

    @staticmethod
    def _manual_work(events, paid_orders) -> dict:
        logs = events.filter(event_type=OrderEvent.Type.MANUAL_WORK_LOGGED)
        minutes_by_order = Counter()
        minutes_by_activity = Counter()
        for order_id, payload in logs.values_list("order_id", "payload"):
            minutes = int((payload or {}).get("minutes") or 0)
            minutes_by_order[order_id] += minutes
            minutes_by_activity[(payload or {}).get("activity") or "other"] += minutes
        total = sum(minutes_by_order.values())
        logged_orders = len(minutes_by_order)
        paid = len(paid_orders)
        return {
            "entries": logs.count(),
            "orders_with_logs": logged_orders,
            "total_minutes": total,
            "minutes_per_logged_order": _ratio(total, logged_orders),
            "minutes_per_paid_order": _ratio(total, paid),
            "paid_orders_without_logs": len(set(paid_orders) - set(minutes_by_order)),
            "minutes_by_activity": dict(minutes_by_activity),
        }

    # -- delivery -------------------------------------------------------------

    @staticmethod
    def _delivery(reached, status_now) -> dict:
        return {
            "orders_delivered": len(reached.get(Order.Status.DELIVERED, set())),
            "orders_ready_for_delivery": len(reached.get(Order.Status.READY_FOR_DELIVERY, set())),
        }
