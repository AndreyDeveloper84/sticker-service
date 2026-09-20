"""Order unit economics (DRF-2111, PR-B).

``OrderEconomics.compute(order)`` folds the facts recorded by other services
into one read-only structure for the console — no provider calls, no
arithmetic in templates, no conversions and no defaults:

- revenue: the confirmed payment's amount, in ITS currency (XTR stays XTR);
  a REFUNDED payment is a proven 0 — revenue 0 with ``refund`` naming the
  returned amount (UNKNOWN != 0: a refund is evidence, so 0 is allowed);
- payment_fee: ``Payment.metadata["fee"]`` written at confirmation from the
  provider's own ``income_amount`` (YooKassa); anything else is UNKNOWN;
- ai: ``generation_cost`` snapshots by stage — PREVIEW / REVISION / FULL
  (initial attempts) / REGENERATION (retry_failed, regenerate, force_retry);
- manual: operator minutes from ``ManualWorkLog`` and the rate snapshot the
  log carries (``manual_work_snapshot``); a log without a snapshot makes the
  manual cost "не настроено", never 0;
- known_variable_cost_minor = known AI + known fee + known manual, RUB only;
- known_contribution_minor only when revenue is in RUB, else NOT_COMPUTABLE;
- unknown_components: the reasons the figures above are incomplete; the UI
  must show them next to the contribution.

Query strategy: three relations per order — ``generation_jobs``, ``payments``
and ``events`` — each read once via ``.all()``, so a queryset that prefetches
them (the console does) renders the block with zero extra queries and a bare
order costs exactly three, independent of the number of jobs.
"""

from __future__ import annotations

from django.utils import timezone

from apps.core.console_text import slot_title
from apps.core.models import GenerationJob, Order, OrderEvent, Payment
from apps.core.services import generation_cost
from apps.core.services.budget import setting

RUB = "RUB"
UNKNOWN = "UNKNOWN"
NOT_COMPUTABLE = "NOT_COMPUTABLE"
PROVIDER_CONFIRMED = "PROVIDER_CONFIRMED"

STAGE_PREVIEW = "preview"
STAGE_REVISION = "revision"
STAGE_FULL = "full"
STAGE_REGENERATION = "regeneration"
STAGES = (STAGE_PREVIEW, STAGE_REVISION, STAGE_FULL, STAGE_REGENERATION)
REGENERATION_MODES = {
    generation_cost.MODE_RETRY_FAILED,
    generation_cost.MODE_REGENERATE,
    generation_cost.MODE_FORCE_RETRY,
}

RATE_KEY = "rate"
RATE_SOURCE_CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"
RATE_SOURCE_NOT_CONFIGURED = "not_configured"
RATE_SOURCE_NO_LOGS = "no_logs"


# ------------------------------------------------------------ operator rate


def operator_rate_rub() -> float | None:
    """``PILOT_OPERATOR_COST_PER_HOUR_RUB`` (float, rubles per hour) or None.
    Nothing is assumed when it is unset or malformed."""
    raw = setting("PILOT_OPERATOR_COST_PER_HOUR_RUB")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def manual_work_snapshot(minutes: int, *, now=None) -> dict | None:
    """Rate snapshot stored in ``ManualWorkLog.payload["rate"]`` at logging
    time; None when no rate is configured (the log then stays "не настроено"
    forever — a rate configured tomorrow never re-prices it)."""
    rate = operator_rate_rub()
    if rate is None:
        return None
    now = now or timezone.now()
    rate_minor = int(round(rate * 100))
    label = setting("PILOT_OPERATOR_RATE_VERSION")
    version = str(label).strip() if label else f"{rate:g}@{timezone.localtime(now).date().isoformat()}"
    return {
        "rate_minor_per_hour": rate_minor,
        "rate_version": version,
        "cost_minor": int(round(rate_minor * int(minutes) / 60)),
        "currency": RUB,
        "rate_source": RATE_SOURCE_CONFIG_SNAPSHOT,
        "captured_at": now.isoformat(),
    }


# ------------------------------------------------------------ payment fee


def payment_fee(payment: Payment | None) -> dict:
    """{"amount_minor", "currency", "source"} from the provider-confirmed fee
    evidence on the payment, else source UNKNOWN."""
    fee = (payment.metadata or {}).get("fee") if payment is not None else None
    if (
        isinstance(fee, dict)
        and fee.get("source") == PROVIDER_CONFIRMED
        and isinstance(fee.get("amount_minor"), int)
        and fee.get("currency")
    ):
        return {
            "amount_minor": int(fee["amount_minor"]),
            "currency": str(fee["currency"]),
            "source": PROVIDER_CONFIRMED,
        }
    return {"amount_minor": None, "currency": None, "source": UNKNOWN}


# ------------------------------------------------------------ economics


def _stage_of(job: GenerationJob) -> str:
    if job.task_type == GenerationJob.TaskType.PREVIEW:
        return STAGE_PREVIEW
    if job.task_type == GenerationJob.TaskType.REVISION:
        return STAGE_REVISION
    cost = generation_cost.job_cost(job.input_metadata)
    mode = (cost or {}).get("mode") or generation_cost.MODE_INITIAL
    return STAGE_REGENERATION if mode in REGENERATION_MODES else STAGE_FULL


def _slot_row(order: Order, job: GenerationJob) -> dict:
    cost = generation_cost.job_cost(job.input_metadata)
    return {
        "slot_key": job.slot_key,
        "title": slot_title(order, job.slot_key) if job.slot_key else "",
        "attempt": job.attempt,
        "status": job.status,
        "mode": (cost or {}).get("mode"),
        "billing_outcome": (cost or {}).get("billing_outcome"),
        "billable": (cost or {}).get("billable"),
        "cost_minor": cost["cost_minor"] if generation_cost.is_known(cost) else None,
    }


class OrderEconomics:
    @classmethod
    def compute(cls, order: Order) -> dict:
        jobs = [job for job in order.generation_jobs.all() if job.started_at is not None]
        payments = list(order.payments.all())
        logs = [
            event for event in order.events.all()
            if event.event_type == OrderEvent.Type.MANUAL_WORK_LOGGED
        ]

        payment = cls._revenue_payment(payments)
        refund = None
        if payment is not None and payment.status == Payment.Status.REFUNDED:
            details = (payment.metadata or {}).get("refund") or {}
            refund = {
                "amount_minor": payment.amount_minor,
                "currency": payment.currency,
                "refunded_at": details.get("refunded_at"),
                "reason": str(details.get("reason") or ""),
            }
        revenue = (
            {"amount_minor": 0 if refund else payment.amount_minor, "currency": payment.currency}
            if payment is not None else None
        )
        fee = payment_fee(payment)
        ai = cls._ai(order, jobs)
        manual = cls._manual(logs)

        unknown = []
        if payment is None:
            unknown.append("no_confirmed_payment")
        elif fee["source"] != PROVIDER_CONFIRMED:
            unknown.append("payment_fee_unknown")
        total = ai["total"]
        if total["unknown_price_count"]:
            unknown.append(f"ai_unknown_price:{total['unknown_price_count']}")
        if total["possibly_billable_count"]:
            unknown.append(f"ai_possibly_billable:{total['possibly_billable_count']}")
        if manual["rate_source"] == RATE_SOURCE_NOT_CONFIGURED:
            unknown.append("manual_rate_not_configured")
        if revenue is not None and revenue["currency"] != RUB:
            unknown.append(f"revenue_currency_{revenue['currency'].lower()}")

        known_variable = total["known_cost_minor"]
        if fee["source"] == PROVIDER_CONFIRMED and fee["currency"] == RUB:
            known_variable += fee["amount_minor"]
        if manual["cost_minor"] is not None:
            known_variable += manual["cost_minor"]

        if revenue is not None and revenue["currency"] == RUB:
            contribution = revenue["amount_minor"] - known_variable
        else:
            contribution = NOT_COMPUTABLE

        return {
            "product": {"code": order.product.code, "name": order.product.name},
            "channel": order.channel_identity.channel if order.channel_identity_id else "",
            "payment": (
                {"provider": payment.provider, "status": payment.status, "confirmed_at": payment.confirmed_at}
                if payment is not None else None
            ),
            "revenue": revenue,
            "refund": refund,
            "payment_fee": fee,
            "ai": ai,
            "manual": manual,
            "currency": RUB,
            "known_variable_cost_minor": known_variable,
            "known_contribution_minor": contribution,
            "unknown_components": unknown,
        }

    @staticmethod
    def _revenue_payment(payments) -> Payment | None:
        """The payment the customer made: CONFIRMED, or REFUNDED (it was
        confirmed once — the refund is reported, not hidden)."""
        made = [p for p in payments if p.status in (Payment.Status.CONFIRMED, Payment.Status.REFUNDED)]
        if not made:
            return None
        return max(made, key=lambda p: (p.confirmed_at or timezone.now(), p.pk))

    @staticmethod
    def _ai(order: Order, jobs) -> dict:
        by_stage = {stage: [] for stage in STAGES}
        for job in jobs:
            by_stage[_stage_of(job)].append(job)
        stages = {}
        for stage, stage_jobs in by_stage.items():
            summary = generation_cost.aggregate(job.input_metadata for job in stage_jobs)
            item = {
                "calls": summary["jobs"],
                "known_cost_minor": summary["known_cost_minor"],
                "known_count": summary["known_count"],
                "possibly_billable_count": summary["possibly_billable_count"],
                "unknown_price_count": summary["unknown_price_count"],
                "not_billable_count": summary["not_billable_count"],
            }
            if stage in (STAGE_FULL, STAGE_REGENERATION):
                item["slots"] = [
                    _slot_row(order, job)
                    for job in sorted(stage_jobs, key=lambda j: (j.slot_key, j.attempt, j.pk))
                ]
            stages[stage] = item
        stages["total"] = generation_cost.aggregate(job.input_metadata for job in jobs)
        return stages

    @staticmethod
    def _manual(logs) -> dict:
        minutes = 0
        cost = 0
        priced = 0
        for log in logs:
            payload = log.payload or {}
            minutes += int(payload.get("minutes") or 0)
            rate = payload.get(RATE_KEY)
            if isinstance(rate, dict) and isinstance(rate.get("cost_minor"), int) and rate.get("currency") == RUB:
                cost += int(rate["cost_minor"])
                priced += 1
        if not logs:
            return {"minutes": 0, "entries": 0, "cost_minor": 0, "rate_source": RATE_SOURCE_NO_LOGS}
        if priced != len(logs):
            # at least one log was written without a configured rate: the
            # manual cost of this order is not known — "не настроено", not 0
            return {"minutes": minutes, "entries": len(logs), "cost_minor": None,
                    "rate_source": RATE_SOURCE_NOT_CONFIGURED}
        return {"minutes": minutes, "entries": len(logs), "cost_minor": cost,
                "rate_source": RATE_SOURCE_CONFIG_SNAPSHOT}


# ------------------------------------------------------------ formatting


def money(minor: int, currency: str = RUB) -> str:
    if currency == RUB:
        return f"{minor / 100:.2f}".replace(".", ",") + " ₽"
    return f"{minor} {currency}"


STAGE_TITLES = {
    STAGE_PREVIEW: "превью",
    STAGE_REVISION: "правки",
    STAGE_FULL: "производство",
    STAGE_REGENERATION: "перегенерации",
}

UNKNOWN_TITLES = {
    "no_confirmed_payment": "нет подтверждённого платежа",
    "payment_fee_unknown": "комиссия платежа неизвестна",
    "manual_rate_not_configured": "ставка оператора не настроена",
}


def unknown_component_text(code: str) -> str:
    if code in UNKNOWN_TITLES:
        return UNKNOWN_TITLES[code]
    name, _sep, count = code.partition(":")
    if name == "ai_unknown_price":
        return f"AI-вызовов без цены: {count}"
    if name == "ai_possibly_billable":
        return f"возможно платных AI-вызовов: {count}"
    if name.startswith("revenue_currency_"):
        return f"выручка в {name.rsplit('_', 1)[-1].upper()}, конверсии нет"
    return code
