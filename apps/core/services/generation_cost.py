"""Generation cost accounting foundation (DRF-2111, PR-A).

Every billable attempt gets an immutable **price snapshot** in
``GenerationJob.input_metadata["cost"]``, written by the job-creating
transaction (the same one Budget Guard runs in), BEFORE the provider is
called. The provider outcome is appended afterwards (``billing_outcome``,
``billable``, ``request_id``) without touching the price fields, so a tariff
change tomorrow can never change the cost of yesterday's job.

Tariff: ``PILOT_IMAGE_CALL_COST_RUB`` (float ₽ per call) + optional
``PILOT_IMAGE_PRICING_VERSION`` (label; default ``"<price>@<YYYY-MM-DD>"`` at
snapshot time). No tariff configured → ``cost_source=UNKNOWN``,
``cost_minor=null`` — never a default price, never 0.

Invariants (owner document):
- UNKNOWN != 0 — an unknown price or an unknown billing outcome is reported
  as unknown, never folded into a sum;
- FAILED != FREE — only a failure that provably never reached the provider
  (``before_provider``: connect/transport, geo refusal) is ``billable=false``;
  ``moderation_blocked`` / ``api_error`` / ``timeout_ambiguous`` / ``unknown``
  are ``billable=null`` (provider semantics are not proven by our code);
- historical jobs without a snapshot are UNKNOWN; backfill is forbidden;
- analytics never calls the provider.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from apps.core.models import GenerationJob
from apps.core.services.budget import call_cost_rub, setting

COST_KEY = "cost"
CURRENCY = "RUB"
UNIT_CALL = "call"


class CostSource:
    PROVIDER_CONFIRMED = "PROVIDER_CONFIRMED"  # reserved: only via an imported provider statement
    CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"  # tariff from config at attempt time
    ESTIMATED = "ESTIMATED"  # reserved, not used in PR-A
    UNKNOWN = "UNKNOWN"


class BillingOutcome:
    PENDING = "pending"  # attempt created, provider not answered yet
    SUCCESS = "success"
    MODERATION_BLOCKED = "moderation_blocked"
    BEFORE_PROVIDER = "before_provider"  # request provably never accepted
    API_ERROR = "api_error"  # definitive 4xx/5xx after acceptance
    TIMEOUT_AMBIGUOUS = "timeout_ambiguous"  # post-submit timeout / stale RUNNING
    UNKNOWN = "unknown"


# failure_class (image_providers.classify_failure / describe_provider_failure)
# → (billing_outcome, billable). None = UNKNOWN.
FAILURE_OUTCOMES = {
    "transport": (BillingOutcome.BEFORE_PROVIDER, False),
    "geo": (BillingOutcome.BEFORE_PROVIDER, False),
    "moderation": (BillingOutcome.MODERATION_BLOCKED, None),
    "api": (BillingOutcome.API_ERROR, None),
    "ambiguous": (BillingOutcome.TIMEOUT_AMBIGUOUS, None),
    # async C-1: a queued job nobody picked up, dequeued by the operator —
    # the provider was never called.
    "queue_lost": (BillingOutcome.BEFORE_PROVIDER, False),
}

PRICE_FIELDS = (
    "provider", "endpoint", "model", "unit", "pricing_version", "unit_price_minor",
    "currency", "cost_minor", "cost_source", "captured_at", "task_type", "mode", "qc_attempt",
)
OUTCOME_FIELDS = ("billing_outcome", "billable", "request_id", "moderation_stage", "outcome_at")

MODE_INITIAL = "initial"
MODE_RETRY_FAILED = "retry_failed"
MODE_REGENERATE = "regenerate"
MODE_FORCE_RETRY = "force_retry"

PROVIDER_ENDPOINTS = {"openai": "images.edit", "nodule": "images.generations"}


def pricing_version(price: float, now: datetime) -> str:
    label = setting("PILOT_IMAGE_PRICING_VERSION")
    if label:
        return str(label).strip()
    return f"{price:g}@{timezone.localtime(now).date().isoformat()}"


def cost_snapshot(*, provider, task_type: str, mode: str = MODE_INITIAL, qc_attempt=None, now=None) -> dict:
    """The immutable price part of the snapshot, taken at attempt creation.

    Reads only configuration and the provider object (name / model); never
    calls the provider.
    """
    now = now or timezone.now()
    name = str(getattr(provider, "name", "") or "")
    price = call_cost_rub()
    snapshot = {
        "provider": name,
        "endpoint": str(getattr(provider, "endpoint", "") or PROVIDER_ENDPOINTS.get(name, "")),
        "model": str(getattr(provider, "model", "") or ""),
        "task_type": str(task_type),
        "mode": mode,
        "qc_attempt": qc_attempt,
        "unit": UNIT_CALL,
        "currency": CURRENCY,
        "captured_at": now.isoformat(),
        # outcome part — filled by record_outcome()
        "billing_outcome": BillingOutcome.PENDING,
        "billable": None,
        "request_id": "",
    }
    if price is None:
        snapshot.update({"pricing_version": None, "unit_price_minor": None, "cost_minor": None,
                         "cost_source": CostSource.UNKNOWN})
    else:
        unit_minor = int(round(price * 100))
        snapshot.update({"pricing_version": pricing_version(price, now), "unit_price_minor": unit_minor,
                         "cost_minor": unit_minor, "cost_source": CostSource.CONFIG_SNAPSHOT})
    return snapshot


def with_outcome(cost: dict | None, *, outcome: str, billable, request_id: str = "", moderation_stage: str = "") -> dict:
    """Return the snapshot with the outcome fields set. Price fields are
    copied verbatim — this function cannot change them by construction."""
    base = dict(cost or {})
    updated = {key: base[key] for key in PRICE_FIELDS if key in base}
    updated.update({key: base[key] for key in base if key not in PRICE_FIELDS and key not in OUTCOME_FIELDS})
    updated.update(
        {
            "billing_outcome": outcome,
            "billable": billable,
            "request_id": str(request_id or base.get("request_id") or ""),
            "outcome_at": timezone.now().isoformat(),
        }
    )
    if moderation_stage:
        updated["moderation_stage"] = str(moderation_stage)
    return updated


def outcome_for_failure(failure: dict | None) -> tuple[str, bool | None]:
    failure_class = str((failure or {}).get("failure_class") or "")
    return FAILURE_OUTCOMES.get(failure_class, (BillingOutcome.UNKNOWN, None))


def apply_success(job: GenerationJob, result_metadata: dict | None) -> None:
    """Mutates job.input_metadata in place (caller saves)."""
    cost = (job.input_metadata or {}).get(COST_KEY)
    if cost is None:
        return  # historical job without a snapshot stays UNKNOWN — no backfill
    job.input_metadata = {
        **(job.input_metadata or {}),
        COST_KEY: with_outcome(
            cost,
            outcome=BillingOutcome.SUCCESS,
            billable=True,
            request_id=str((result_metadata or {}).get("request_id") or ""),
        ),
    }


def apply_failure(job: GenerationJob, failure: dict | None) -> None:
    """Mutates job.input_metadata in place (caller saves)."""
    cost = (job.input_metadata or {}).get(COST_KEY)
    if cost is None:
        return
    outcome, billable = outcome_for_failure(failure)
    job.input_metadata = {
        **(job.input_metadata or {}),
        COST_KEY: with_outcome(
            cost,
            outcome=outcome,
            billable=billable,
            request_id=str((failure or {}).get("request_id") or ""),
            moderation_stage=str((failure or {}).get("moderation_stage") or ""),
        ),
    }


# ------------------------------------------------------------- reading


def job_cost(input_metadata: dict | None) -> dict | None:
    """The snapshot of a job, or None for a historical job (UNKNOWN)."""
    cost = (input_metadata or {}).get(COST_KEY)
    return dict(cost) if isinstance(cost, dict) else None


def is_known(cost: dict | None) -> bool:
    return bool(cost) and cost.get("cost_source") == CostSource.CONFIG_SNAPSHOT and cost.get("billable") is True


def aggregate(input_metadatas) -> dict:
    """Aggregate snapshots of started jobs.

    - known_cost_minor: sum(cost_minor) where billable is True and the price
      was a CONFIG_SNAPSHOT (the only sum that is a fact);
    - known_count: jobs behind that sum;
    - not_billable_count: billable is False (never reached the provider);
    - possibly_billable_count: billable is null (outcome unknown / pending);
    - unknown_price_count: no snapshot or cost_source UNKNOWN, and not
      provably free.
    """
    known_minor = 0
    known = 0
    not_billable = 0
    possibly = 0
    unknown_price = 0
    total = 0
    for metadata in input_metadatas:
        total += 1
        cost = job_cost(metadata)
        if cost is None:
            unknown_price += 1
            possibly += 1
            continue
        billable = cost.get("billable")
        if billable is False:
            not_billable += 1
            continue
        if billable is None:
            possibly += 1
        if cost.get("cost_source") != CostSource.CONFIG_SNAPSHOT or cost.get("cost_minor") is None:
            unknown_price += 1
            continue
        if billable is True:
            known += 1
            known_minor += int(cost["cost_minor"])
    return {
        "currency": CURRENCY,
        "jobs": total,
        "known_cost_minor": known_minor,
        "known_count": known,
        "not_billable_count": not_billable,
        "possibly_billable_count": possibly,
        "unknown_price_count": unknown_price,
    }


def format_known_cost(summary: dict) -> str:
    """Operator-facing figure: «7,42 ₽ (известно по 3 вызовам)» / «неизвестна» /
    «нет вызовов» / «0 ₽ (провайдер не принял N)». A 0 is printed only with
    the evidence behind it (DRF-2111 C1)."""
    known = summary.get("known_count", 0)
    jobs = summary.get("jobs", summary.get("calls", 0))  # aggregate() or an order_economics stage
    if not known:
        if not jobs:
            return "нет вызовов"  # proven: nothing was generated
        if summary.get("not_billable_count", 0) == jobs:
            # every call provably never reached the provider: a fact, not an
            # unknown — 0 with the reason spelled out
            return f"0 ₽ (провайдер не принял {jobs})"
        return "неизвестна"
    rub = summary["known_cost_minor"] / 100
    text = f"{rub:.2f}".replace(".", ",") + " ₽"
    return f"{text} (известно по {known} вызовам)" if known != jobs else text
