"""Pilot analytics for the «Метрики Pilot» console page and export (DRF-2111, PR-C).

One source of truth on top of the services that already own the facts:
``OrderEconomics`` (per-order revenue / AI / manual / fee / contribution),
``generation_cost`` (job price snapshots) and ``BudgetService`` (limits and
windows). Nothing here re-prices anything, calls a provider or converts
currencies; UNKNOWN stays UNKNOWN.

Period: half-open ``[start, end)`` in the business time zone (Europe/Moscow,
``settings.TIME_ZONE``), built by :func:`period_for` from a preset
(``today`` / ``7d`` / ``30d``) or two dates (``custom``), day boundaries at
local midnight — the same convention as ``BudgetService.day_window``.

Cohort: the orders **created** in the period (their whole life is folded in,
whenever it happened). The ORDERS block is the exception: it counts
``order.status_changed`` events that happened in the period, whatever the
order's creation date, so a day's "delivered / cancelled" is what actually
happened that day.

Query strategy (fixed, independent of the number of orders / jobs):
1 orders (select_related product, channel_identity, revision)
+ 4 prefetches (generation_jobs, payments, events, qc_reports)
+ 1 status events in the period + 1 active products
+ BudgetService.summary() (4). Per-order economics then run on prefetched
relations with zero extra queries.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from apps.core.models import GenerationJob, Order, OrderEvent, Payment, Product, QcReport
from apps.core.services import generation_cost
from apps.core.services.budget import BudgetService, safe_limit
from apps.core.services.order_economics import (
    NOT_COMPUTABLE,
    PROVIDER_CONFIRMED,
    RATE_SOURCE_NO_LOGS,
    RATE_SOURCE_NOT_CONFIGURED,
    RUB,
    STAGES,
    OrderEconomics,
)

PRESETS = ("today", "7d", "30d", "custom")
PRESET_DAYS = {"today": 1, "7d": 7, "30d": 30}

BUDGET_WARNING_PERCENT = 80
BUDGET_LIMIT_PERCENT = 100

# Statuses counted as "failed" in ORDERS: the existing terminal FAILED only
# (no new statuses are invented here).
FAILED_STATUSES = (Order.Status.FAILED,)

# unknown_components that are COST gaps (revenue / payment ones are not).
COST_UNKNOWN_COMPONENTS = {
    "payment_fee_unknown", "ai_unknown_price", "ai_possibly_billable", "manual_rate_not_configured",
}

# §21 export columns, in order.
EXPORT_COLUMNS = (
    "order_id", "created_at", "paid_at", "delivered_at", "channel", "product", "currency", "revenue",
    "generation_calls", "preview_cost", "revision_cost", "full_cost", "regeneration_cost", "ai_total",
    "manual_minutes", "manual_cost", "payment_fee", "known_variable_cost", "known_contribution",
    "unknown_cost_components",
)


# ------------------------------------------------------------------ period


@dataclass(frozen=True)
class Period:
    preset: str
    start: datetime  # aware, local midnight
    end: datetime  # aware, exclusive

    @property
    def start_date(self) -> date:
        return timezone.localtime(self.start).date()

    @property
    def end_date(self) -> date:
        """Last calendar day of the period (inclusive)."""
        return timezone.localtime(self.end).date() - timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.start_date.isoformat()}_{self.end_date.isoformat()}"

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict:
        return {
            "preset": self.preset,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "timezone": str(timezone.get_current_timezone()),
        }


def local_midnight(day: date) -> datetime:
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(day, time.min), tz)


def period_for(preset: str = "today", *, start: date | None = None, end: date | None = None, now=None) -> Period:
    """Build the half-open period. ``custom`` needs both dates (inclusive
    calendar days); an inverted range is swapped; unknown presets → today."""
    today = timezone.localtime(now or timezone.now()).date()
    if preset == "custom" and start and end:
        if end < start:
            start, end = end, start
        return Period("custom", local_midnight(start), local_midnight(end + timedelta(days=1)))
    if preset not in PRESET_DAYS:
        preset = "today"
    days = PRESET_DAYS[preset]
    return Period(preset, local_midnight(today - timedelta(days=days - 1)), local_midnight(today + timedelta(days=1)))


# ------------------------------------------------------------------ helpers


def _percent(numerator: int, denominator: int):
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def _delivered_at(order: Order):
    moments = [
        event.created_at
        for event in order.events.all()
        if event.event_type == OrderEvent.Type.STATUS_CHANGED and event.to_status == Order.Status.DELIVERED
    ]
    return min(moments) if moments else None


def _paid_at(order: Order):
    # a refunded payment was confirmed once: the order did reach "paid"
    made = (Payment.Status.CONFIRMED, Payment.Status.REFUNDED)
    moments = [p.confirmed_at for p in order.payments.all() if p.status in made and p.confirmed_at]
    return min(moments) if moments else None


def _customer_approved(order: Order) -> bool:
    return any(event.event_type == OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED for event in order.events.all())


def _has_revision(order: Order) -> bool:
    try:
        return order.revision is not None
    except Order.revision.RelatedObjectDoesNotExist:
        return False


def _has_succeeded_preview(order: Order) -> bool:
    return any(
        job.task_type == GenerationJob.TaskType.PREVIEW and job.status == GenerationJob.Status.SUCCEEDED
        for job in order.generation_jobs.all()
    )


def _moderation_failures(order: Order) -> int:
    count = 0
    for job in order.generation_jobs.all():
        cost = generation_cost.job_cost(job.input_metadata)
        if cost is not None:
            if cost.get("billing_outcome") == generation_cost.BillingOutcome.MODERATION_BLOCKED:
                count += 1
        elif (job.output_metadata or {}).get("failure_class") == "moderation":
            count += 1  # historical job without a snapshot
    return count


def _qc_retries(order: Order) -> int:
    return sum(
        1 for report in order.qc_reports.all()
        if report.status == QcReport.Status.FAILED and report.retry_slots
    )


def _ai_fully_known(eco: dict) -> bool:
    total = eco["ai"]["total"]
    return not total["unknown_price_count"] and not total["possibly_billable_count"]


# ------------------------------------------------------------------ service


class PilotAnalyticsService:
    def __init__(self, period: Period):
        self.period = period

    # -- data ---------------------------------------------------------------

    def orders(self):
        return (
            Order.objects.filter(created_at__gte=self.period.start, created_at__lt=self.period.end)
            .select_related("product", "channel_identity", "revision")
            .prefetch_related("generation_jobs", "payments", "events", "qc_reports")
            .order_by("created_at", "pk")
        )

    def rows(self, orders=None) -> list[dict]:
        """One entry per cohort order: the order, its economics and timestamps."""
        rows = []
        for order in (orders if orders is not None else self.orders()):
            rows.append({
                "order": order,
                "eco": OrderEconomics.compute(order),
                "paid_at": _paid_at(order),
                "delivered_at": _delivered_at(order),
            })
        return rows

    def snapshot(self) -> dict:
        rows = self.rows()
        return {
            "period": self.period.as_dict(),
            "orders": self._orders_block(),
            "funnel": self._funnel(rows),
            "revenue": self._revenue(rows),
            "ai_cost": self._ai_cost(rows),
            "quality": self._quality(rows),
            "operations": self._operations(rows),
            "unit_economics": self._unit_economics(rows),
            "budget": self.budget(),
        }

    # -- ORDERS (events in the period) ---------------------------------------

    def _orders_block(self) -> dict:
        events = OrderEvent.objects.filter(
            event_type=OrderEvent.Type.STATUS_CHANGED,
            created_at__gte=self.period.start, created_at__lt=self.period.end,
        ).values_list("order_id", "to_status")
        by_status = defaultdict(set)
        for order_id, to_status in events:
            by_status[to_status].add(order_id)
        started = Order.objects.filter(created_at__gte=self.period.start, created_at__lt=self.period.end).count()
        return {
            "started": started,
            "paid": len(by_status.get(Order.Status.PAID, ())),
            "delivered": len(by_status.get(Order.Status.DELIVERED, ())),
            "cancelled": len(by_status.get(Order.Status.CANCELLED, ())),
            "failed": len(set().union(*(by_status.get(status, set()) for status in FAILED_STATUSES))),
        }

    # -- FUNNEL (cohort) ------------------------------------------------------

    @staticmethod
    def _funnel(rows) -> dict:
        started = len(rows)
        paid = sum(1 for r in rows if r["paid_at"] is not None)
        preview = sum(1 for r in rows if r["paid_at"] is not None and _has_succeeded_preview(r["order"]))
        approved = sum(1 for r in rows if r["paid_at"] is not None and _customer_approved(r["order"]))
        delivered = sum(1 for r in rows if r["delivered_at"] is not None)
        steps = [("start", started), ("payment", paid), ("preview", preview), ("approval", approved),
                 ("delivered", delivered)]
        result = []
        previous = None
        for name, count in steps:
            result.append({
                "step": name,
                "count": count,
                "conversion_percent": _percent(count, previous) if previous is not None else None,
            })
            previous = count
        return {"steps": result, "overall_percent": _percent(delivered, started)}

    # -- REVENUE (per currency, never mixed) ----------------------------------

    @staticmethod
    def _revenue(rows) -> dict:
        cells = {}
        totals = {}
        refunded = {}
        for r in rows:
            revenue = r["eco"]["revenue"]
            if revenue is None:
                continue
            refund = r["eco"]["refund"]
            if refund:
                # proven 0: not a revenue cell, listed apart as «возвращено»
                cell = refunded.setdefault(refund["currency"], {"currency": refund["currency"], "orders": 0, "amount_minor": 0})
                cell["orders"] += 1
                cell["amount_minor"] += refund["amount_minor"]
                continue
            key = (r["order"].product.code, r["eco"]["channel"], revenue["currency"])
            cell = cells.setdefault(key, {
                "product": key[0], "channel": key[1], "currency": key[2], "orders": 0, "amount_minor": 0,
            })
            cell["orders"] += 1
            cell["amount_minor"] += revenue["amount_minor"]
            total = totals.setdefault(revenue["currency"], {"currency": revenue["currency"], "orders": 0, "amount_minor": 0})
            total["orders"] += 1
            total["amount_minor"] += revenue["amount_minor"]
        return {
            "cells": [cells[key] for key in sorted(cells)],
            "totals": [totals[key] for key in sorted(totals)],
            "refunded": [refunded[key] for key in sorted(refunded)],
        }

    # -- AI COST --------------------------------------------------------------

    @staticmethod
    def _ai_cost(rows) -> dict:
        jobs = [job.input_metadata for r in rows for job in r["order"].generation_jobs.all() if job.started_at]
        total = generation_cost.aggregate(jobs)
        stages = {}
        for stage in STAGES:
            agg = Counter()
            for r in rows:
                item = r["eco"]["ai"][stage]
                for key in ("calls", "known_cost_minor", "known_count", "possibly_billable_count",
                            "unknown_price_count", "not_billable_count"):
                    agg[key] += item[key]
            stages[stage] = dict(agg)
        # DRF-2111 C1: only paid orders with at least one provider call whose
        # cost is fully known take part — an order without calls is not
        # evidence of a 0 ₽ generation cost
        paid_known = [
            r["eco"]["ai"]["total"]["known_cost_minor"]
            for r in rows
            if r["paid_at"] is not None and r["eco"]["ai"]["total"]["jobs"] and _ai_fully_known(r["eco"])
        ]
        return {
            "currency": RUB,
            "total": total,
            "stages": stages,
            "paid_orders": sum(1 for r in rows if r["paid_at"] is not None),
            "paid_orders_with_known_cost": len(paid_known),
            "known_cost_minor_per_paid_order_avg": _mean(paid_known),
            "known_cost_minor_per_paid_order_median": _median(paid_known),
        }

    # -- QUALITY --------------------------------------------------------------

    @staticmethod
    def _quality(rows) -> dict:
        with_preview = [r for r in rows if _has_succeeded_preview(r["order"])]
        approved_first_try = sum(
            1 for r in with_preview if _customer_approved(r["order"]) and not _has_revision(r["order"])
        )
        revisions = sum(1 for r in with_preview if _has_revision(r["order"]))
        revision_jobs = sum(r["eco"]["ai"]["revision"]["calls"] for r in rows)
        paid = [r for r in rows if r["paid_at"] is not None]
        regenerations = sum(r["eco"]["ai"]["regeneration"]["calls"] for r in paid)
        return {
            "orders_with_preview": len(with_preview),
            "preview_first_try_accepted": approved_first_try,
            "preview_first_try_percent": _percent(approved_first_try, len(with_preview)),
            "orders_with_revision": revisions,
            "revision_jobs": revision_jobs,
            "revisions_per_order": _mean([1 if _has_revision(r["order"]) else 0 for r in with_preview]),
            "regeneration_calls": regenerations,
            "regenerations_per_paid_order": _mean(
                [r["eco"]["ai"]["regeneration"]["calls"] for r in paid]
            ),
            "moderation_failures": sum(_moderation_failures(r["order"]) for r in rows),
            "qc_retries": sum(_qc_retries(r["order"]) for r in rows),
        }

    # -- OPERATIONS -----------------------------------------------------------

    @staticmethod
    def _operations(rows) -> dict:
        paid = [r for r in rows if r["paid_at"] is not None]
        logged = [r for r in rows if r["eco"]["manual"]["entries"]]
        paid_logged = [r for r in paid if r["eco"]["manual"]["entries"]]
        minutes_total = sum(r["eco"]["manual"]["minutes"] for r in logged)
        lead_hours = [
            round((r["delivered_at"] - r["paid_at"]).total_seconds() / 3600, 2)
            for r in rows if r["paid_at"] is not None and r["delivered_at"] is not None
        ]
        return {
            "manual_minutes_total": minutes_total,
            "orders_with_manual_logs": len(logged),
            "manual_minutes_per_logged_order": _mean([r["eco"]["manual"]["minutes"] for r in logged]),
            # DRF-2111 C1: an order without logs has UNKNOWN minutes, not 0 —
            # the per-paid-order mean runs over paid orders WITH logs only and
            # names how many of the paid orders it is based on
            "paid_orders": len(paid),
            "paid_orders_with_logs": len(paid_logged),
            "manual_minutes_per_paid_order": _mean([r["eco"]["manual"]["minutes"] for r in paid_logged]),
            "paid_orders_without_logs": len(paid) - len(paid_logged),
            "lead_time_orders": len(lead_hours),
            "payment_to_delivery_hours_median": _median(lead_hours),
            "payment_to_delivery_hours_avg": _mean(lead_hours),
        }

    # -- UNIT ECONOMICS per product ------------------------------------------

    @staticmethod
    def _unit_economics(rows) -> list[dict]:
        products = {p.code: p for p in Product.objects.filter(is_active=True).order_by("code")}
        for r in rows:  # products of the cohort that are no longer active still show up
            products.setdefault(r["order"].product.code, r["order"].product)
        by_code = defaultdict(list)
        for r in rows:
            by_code[r["order"].product.code].append(r)
        result = []
        for code in sorted(products):
            product = products[code]
            config = product.config or {}
            group = by_code.get(code, [])
            revenue = {}
            known_ai = known_manual = known_fee = 0
            ai_jobs = []
            manual_logged = manual_not_configured = 0
            fee_known = fee_unknown = 0
            contribution_sum = 0
            contribution_orders = 0
            unknown = Counter()
            orders_with_unknown = 0
            for r in group:
                eco = r["eco"]
                if eco["revenue"] is not None:
                    cell = revenue.setdefault(eco["revenue"]["currency"], {"orders": 0, "amount_minor": 0})
                    cell["orders"] += 1
                    cell["amount_minor"] += eco["revenue"]["amount_minor"]
                    fee = eco["payment_fee"]
                    if fee["source"] == PROVIDER_CONFIRMED and fee["currency"] == RUB:
                        known_fee += fee["amount_minor"]
                        fee_known += 1
                    else:
                        fee_unknown += 1
                known_ai += eco["ai"]["total"]["known_cost_minor"]
                ai_jobs.extend(job.input_metadata for job in r["order"].generation_jobs.all() if job.started_at)
                manual = eco["manual"]
                if manual["entries"]:
                    manual_logged += 1
                    if manual["cost_minor"] is None:
                        manual_not_configured += 1
                    else:
                        known_manual += manual["cost_minor"]
                if eco["known_contribution_minor"] != NOT_COMPUTABLE:
                    contribution_sum += eco["known_contribution_minor"]
                    contribution_orders += 1
                if eco["unknown_components"]:
                    orders_with_unknown += 1
                for component in eco["unknown_components"]:
                    unknown[component.split(":", 1)[0]] += 1
            result.append({
                "product": code,
                "name": product.name,
                "is_active": product.is_active,
                "price_minor": config.get("price_minor"),
                "price_currency": str(config.get("currency") or RUB).upper(),
                "orders": len(group),
                "paid_orders": sum(1 for r in group if r["paid_at"] is not None),
                "revenue": [{"currency": cur, **revenue[cur]} for cur in sorted(revenue)],
                # DRF-2111 C1: every known_* sum carries its evidence so the UI
                # can tell "0 because proven" from "0 because nothing is known"
                "known_ai_cost_minor": known_ai,
                "ai": generation_cost.aggregate(ai_jobs),
                "known_manual_cost_minor": known_manual,
                "manual": {"orders_with_logs": manual_logged, "orders_not_configured": manual_not_configured},
                "known_payment_fee_minor": known_fee,
                "payment_fee": {"orders_known": fee_known, "orders_unknown": fee_unknown},
                "known_contribution_minor": contribution_sum if contribution_orders else None,
                "contribution_orders": contribution_orders,
                "orders_with_unknown": orders_with_unknown,
                "unknown_counts": dict(unknown),
            })
        return result

    # -- BUDGET ---------------------------------------------------------------

    @staticmethod
    def budget() -> dict:
        summary = BudgetService().summary()

        def _window(item):
            used, maximum = item["used"], item["max"]
            percent = round(100.0 * used / maximum, 1) if maximum else None
            return {
                "used": used,
                "max": maximum,
                "cost": item["cost"],
                "utilization_percent": percent,
                "warning": bool(maximum) and percent >= BUDGET_WARNING_PERCENT,
                "limit_reached": bool(maximum) and percent >= BUDGET_LIMIT_PERCENT,
            }

        today = _window(summary["today"])
        month = _window(summary["month"])
        return {
            "today": today,
            "month": month,
            "order_limit": safe_limit("PILOT_MAX_IMAGE_CALLS_PER_ORDER"),
            "slot_limit": safe_limit("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"),
            "cost_rub_per_call": summary["cost_rub_per_call"],
            "warning": today["warning"] or month["warning"],
            "limit_reached": today["limit_reached"] or month["limit_reached"],
        }

    # -- EXPORT (§21) ---------------------------------------------------------

    def export_rows(self, rows=None) -> list[dict]:
        """One §21 record per cohort order. UNKNOWN → None (never 0); rubles
        as decimal strings with two places; XTR as an integer with
        currency=XTR. No customer name / contact / photos."""
        return [export_row(r) for r in (rows if rows is not None else self.rows())]


def _rub(minor) -> str | None:
    return None if minor is None else f"{minor / 100:.2f}"


def _stage_cost(item: dict) -> str | None:
    if item["unknown_price_count"] or item["possibly_billable_count"]:
        return None
    return _rub(item["known_cost_minor"])


def _iso(moment) -> str | None:
    return timezone.localtime(moment).isoformat() if moment else None


def export_row(r: dict) -> dict:
    order, eco = r["order"], r["eco"]
    revenue = eco["revenue"]
    ai = eco["ai"]
    manual = eco["manual"]
    fee = eco["payment_fee"]
    if revenue is None:
        currency, amount = None, None
    elif revenue["currency"] == RUB:
        currency, amount = RUB, _rub(revenue["amount_minor"])
    else:
        currency, amount = revenue["currency"], revenue["amount_minor"]
    # no logs → minutes and cost are unknown (empty), not 0 (DRF-2111 C1);
    # logs without a rate → cost unknown, minutes known
    manual_minutes = manual["minutes"] if manual["rate_source"] != RATE_SOURCE_NO_LOGS else None
    manual_cost = (
        _rub(manual["cost_minor"])
        if manual["rate_source"] not in (RATE_SOURCE_NOT_CONFIGURED, RATE_SOURCE_NO_LOGS) else None
    )
    contribution = eco["known_contribution_minor"]
    # DRF-2111 C1: a known variable cost of 0 with cost components missing
    # is "nothing known", not a free order → empty cell
    cost_unknown = any(c.split(":", 1)[0] in COST_UNKNOWN_COMPONENTS for c in eco["unknown_components"])
    known_variable = None if cost_unknown and not eco["known_variable_cost_minor"] else _rub(eco["known_variable_cost_minor"])
    return {
        "order_id": order.pk,
        "created_at": _iso(order.created_at),
        "paid_at": _iso(r["paid_at"]),
        "delivered_at": _iso(r["delivered_at"]),
        "channel": eco["channel"],
        "product": order.product.code,
        "currency": currency,
        "revenue": amount,
        "generation_calls": ai["total"]["jobs"],
        "preview_cost": _stage_cost(ai["preview"]),
        "revision_cost": _stage_cost(ai["revision"]),
        "full_cost": _stage_cost(ai["full"]),
        "regeneration_cost": _stage_cost(ai["regeneration"]),
        "ai_total": _stage_cost(ai["total"]) if _ai_fully_known(eco) else None,
        "manual_minutes": manual_minutes,
        "manual_cost": manual_cost,
        "payment_fee": _rub(fee["amount_minor"]) if fee["source"] == PROVIDER_CONFIRMED and fee["currency"] == RUB else None,
        "known_variable_cost": known_variable,
        "known_contribution": None if contribution == NOT_COMPUTABLE else _rub(contribution),
        "unknown_cost_components": ";".join(eco["unknown_components"]),
    }
