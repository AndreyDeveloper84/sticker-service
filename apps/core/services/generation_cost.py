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

Token pricing (owner GO 2026-09-20, ``cost_source=ESTIMATED``): with the
three rates ``PILOT_TEXT_INPUT_USD_PER_1M`` / ``PILOT_IMAGE_INPUT_USD_PER_1M``
/ ``PILOT_IMAGE_OUTPUT_USD_PER_1M`` configured (+ ``PILOT_TOKEN_PRICING_MODEL``,
``PILOT_TOKEN_PRICING_VERSION``, optional ``PILOT_FX_USD_RUB`` / ``PILOT_FX_DATE``
/ ``PILOT_FX_SOURCE`` — the owner sets the rate, there is no lookup) the
snapshot carries the rates and the fx (``pricing``), and the cost is computed
AFTER the provider answered from the actual ``usage``:
``usd = text_in × r_t + image_in × r_i + out × r_o`` (per 1M, 6 decimals),
``rub_minor = round(usd × fx × 100)`` only when the fx is in the snapshot.
Rates are read from the job's own snapshot only — an env change tomorrow
never re-prices an old job. No ``input_tokens_details`` in the answer →
``usage_split_unknown`` and the cost is UNKNOWN (conservative, never an
upper bound). Token pricing takes precedence over the per-call tariff.

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

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.core.models import GenerationJob
from apps.core.services.budget import call_cost_rub, setting

logger = logging.getLogger(__name__)

COST_KEY = "cost"
CURRENCY = "RUB"
UNIT_CALL = "call"


class CostSource:
    PROVIDER_CONFIRMED = "PROVIDER_CONFIRMED"  # reserved: only via an imported provider statement
    CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"  # tariff from config at attempt time
    ESTIMATED = "ESTIMATED"  # token rates snapshotted at attempt time × actual usage
    UNKNOWN = "UNKNOWN"


KNOWN_SOURCES = (CostSource.CONFIG_SNAPSHOT, CostSource.ESTIMATED)
UNIT_TOKEN = "token"


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
    "pricing",
)
OUTCOME_FIELDS = ("billing_outcome", "billable", "request_id", "moderation_stage", "outcome_at")
# ESTIMATED only: written once by apply_success from the snapshot rates × usage.
ESTIMATE_FIELDS = ("tokens", "usd_estimate", "rub_estimate_minor", "usage_split_unknown")

MODE_INITIAL = "initial"
MODE_RETRY_FAILED = "retry_failed"
MODE_REGENERATE = "regenerate"
MODE_FORCE_RETRY = "force_retry"

PROVIDER_ENDPOINTS = {"openai": "images.edit", "nodule": "images.generations"}


# ---------------------------------------------------------- token pricing


def _float_setting(name: str):
    """(value, valid): None/"" → (None, True); a non-negative float →
    (float, True); anything else → (None, False)."""
    raw = setting(name)
    if raw is None:
        return None, True
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, False
    if value != value or value < 0:  # NaN / negative
        return None, False
    return value, True


def token_pricing(now=None) -> dict | None:
    """The token tariff to snapshot, or None (no token pricing → the per-call
    tariff / UNKNOWN as before). All three rates are required; an invalid
    rate fails closed (None + warning). The fx is optional; an invalid fx
    drops the fx only (USD is still estimated, RUB stays unknown)."""
    rates = {}
    for key, name in (
        ("text_input_rate_usd_per_1m", "PILOT_TEXT_INPUT_USD_PER_1M"),
        ("image_input_rate_usd_per_1m", "PILOT_IMAGE_INPUT_USD_PER_1M"),
        ("image_output_rate_usd_per_1m", "PILOT_IMAGE_OUTPUT_USD_PER_1M"),
    ):
        value, valid = _float_setting(name)
        if not valid:
            logger.warning("generation_cost.token_pricing invalid %s=%r: cost stays UNKNOWN", name, setting(name))
            return None
        if value is None:
            if rates:
                logger.warning("generation_cost.token_pricing %s is not set: cost stays UNKNOWN", name)
            return None
        rates[key] = value
    fx, valid = _float_setting("PILOT_FX_USD_RUB")
    if not valid or (fx is not None and fx == 0):
        logger.warning("generation_cost.token_pricing invalid PILOT_FX_USD_RUB=%r: RUB stays unknown", setting("PILOT_FX_USD_RUB"))
        fx = None
    now = now or timezone.now()
    model = str(setting("PILOT_TOKEN_PRICING_MODEL") or "gpt-image-2").strip()
    version = setting("PILOT_TOKEN_PRICING_VERSION")
    version = str(version).strip() if version else f"openai-pricing@{timezone.localtime(now).date().isoformat()}"
    return {
        "model": model,
        "pricing_version": version,
        **rates,
        "fx_usd_rub": fx,
        "fx_date": str(setting("PILOT_FX_DATE") or "").strip() if fx is not None else "",
        "fx_source": str(setting("PILOT_FX_SOURCE") or "").strip() if fx is not None else "",
        "captured_at": now.isoformat(),
    }


def usage_tokens(usage: dict | None) -> dict | None:
    """{text_input, image_input, output} from a provider ``usage`` with
    ``input_tokens_details``; None when the split is not reported."""
    usage = usage or {}
    details = usage.get("input_tokens_details") or {}
    text = details.get("text_tokens")
    image = details.get("image_tokens")
    output = usage.get("output_tokens")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (text, image, output)):
        return None
    return {"text_input": text, "image_input": image, "output": output}


def estimate_usd(tokens: dict, pricing: dict) -> float:
    usd = (
        tokens["text_input"] * pricing["text_input_rate_usd_per_1m"]
        + tokens["image_input"] * pricing["image_input_rate_usd_per_1m"]
        + tokens["output"] * pricing["image_output_rate_usd_per_1m"]
    ) / 1_000_000
    return round(usd, 6)


def estimate_rub_minor(usd: float, pricing: dict) -> int | None:
    fx = pricing.get("fx_usd_rub")
    if not fx:
        return None
    minor = (Decimal(str(usd)) * Decimal(str(fx)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


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
    pricing = token_pricing(now)
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
    if pricing is not None:
        if price is not None:
            logger.warning(
                "generation_cost: PILOT_IMAGE_CALL_COST_RUB is ignored — token pricing (%s) takes precedence",
                pricing["pricing_version"],
            )
        # the price is computed after the answer, from these rates × usage
        snapshot.update({"unit": UNIT_TOKEN, "pricing_version": pricing["pricing_version"], "unit_price_minor": None,
                         "cost_minor": None, "cost_source": CostSource.ESTIMATED, "pricing": pricing})
    elif price is None:
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
    updated = with_outcome(
        cost,
        outcome=BillingOutcome.SUCCESS,
        billable=True,
        request_id=str((result_metadata or {}).get("request_id") or ""),
    )
    if updated.get("cost_source") == CostSource.ESTIMATED:
        updated.update(estimate_from_usage(updated, (result_metadata or {}).get("usage")))
    job.input_metadata = {**(job.input_metadata or {}), COST_KEY: updated}


def estimate_from_usage(cost: dict, usage: dict | None) -> dict:
    """The ESTIMATED part of the snapshot: snapshot rates × actual usage.
    Without the input split the cost is UNKNOWN (``usage_split_unknown``),
    never an upper bound; without an fx the USD stays and RUB is null."""
    pricing = cost.get("pricing") or {}
    tokens = usage_tokens(usage)
    if tokens is None or not pricing:
        return {"tokens": None, "usd_estimate": None, "rub_estimate_minor": None, "usage_split_unknown": True,
                "cost_minor": None}
    usd = estimate_usd(tokens, pricing)
    rub = estimate_rub_minor(usd, pricing)
    return {"tokens": tokens, "usd_estimate": usd, "rub_estimate_minor": rub, "usage_split_unknown": False,
            "cost_minor": rub}


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
    """A priced, billable job: per-call tariff (CONFIG_SNAPSHOT) or a token
    estimate (ESTIMATED) that reached RUB."""
    return (
        bool(cost)
        and cost.get("cost_source") in KNOWN_SOURCES
        and cost.get("billable") is True
        and cost.get("cost_minor") is not None
    )


def is_estimated(cost: dict | None) -> bool:
    return bool(cost) and cost.get("cost_source") == CostSource.ESTIMATED


def aggregate(input_metadatas) -> dict:
    """Aggregate snapshots of started jobs.

    - known_cost_minor: sum(cost_minor) where billable is True and the price
      is a CONFIG_SNAPSHOT (per call) or an ESTIMATED token cost in RUB —
      split into known_config_minor + known_estimated_minor;
    - known_count: jobs behind that sum (estimated_count of them estimated);
    - usd_estimate_total / usd_known_count: USD estimates of billable
      ESTIMATED jobs (also those without an fx, which have no RUB);
    - tokens: summed usage of the jobs that reported it;
    - not_billable_count: billable is False (never reached the provider);
    - possibly_billable_count: billable is null (outcome unknown / pending);
    - unknown_price_count: no snapshot, cost_source UNKNOWN, or an estimate
      without a RUB figure (no split / no fx), and not provably free.
    """
    known_minor = known_config = known_estimated = 0
    known = estimated = 0
    usd_total = 0.0
    usd_known = 0
    tokens = {"text_input": 0, "image_input": 0, "output": 0}
    tokens_jobs = 0
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
        if is_estimated(cost) and billable is True:
            if cost.get("tokens"):
                tokens_jobs += 1
                for key in tokens:
                    tokens[key] += int(cost["tokens"].get(key) or 0)
            if cost.get("usd_estimate") is not None:
                usd_total += float(cost["usd_estimate"])
                usd_known += 1
        if cost.get("cost_source") not in KNOWN_SOURCES or cost.get("cost_minor") is None:
            unknown_price += 1
            continue
        if billable is True:
            known += 1
            known_minor += int(cost["cost_minor"])
            if is_estimated(cost):
                estimated += 1
                known_estimated += int(cost["cost_minor"])
            else:
                known_config += int(cost["cost_minor"])
    return {
        "currency": CURRENCY,
        "jobs": total,
        "known_cost_minor": known_minor,
        "known_config_minor": known_config,
        "known_estimated_minor": known_estimated,
        "known_count": known,
        "estimated_count": estimated,
        "usd_estimate_total": round(usd_total, 6),
        "usd_known_count": usd_known,
        "tokens": tokens if tokens_jobs else None,
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
    if summary.get("estimated_count"):
        text = "≈ " + text  # an estimate from usage × snapshotted rates, not a tariff
    return f"{text} (известно по {known} вызовам)" if known != jobs else text


def format_ai_total(summary: dict) -> str:
    """Owner wording for the order card: «≈ 12,30 ₽» when every call is
    priced, «известно: ≈ 12,30 ₽ + 2 вызовов с неизвестной стоимостью» when
    some are not; the unknown ones are counted, never priced as 0."""
    known = summary.get("known_count", 0)
    jobs = summary.get("jobs", summary.get("calls", 0))
    unknown = jobs - known - summary.get("not_billable_count", 0)
    if not known or unknown <= 0:
        return format_known_cost(summary)
    rub = summary["known_cost_minor"] / 100
    text = f"{rub:.2f}".replace(".", ",") + " ₽"
    if summary.get("estimated_count"):
        text = "≈ " + text
    return f"известно: {text} + {unknown} вызовов с неизвестной стоимостью"


def format_tokens(tokens: dict | None) -> str:
    if not tokens:
        return ""
    return f"tokens in {tokens['text_input'] + tokens['image_input']} / out {tokens['output']}"


def pricing_caption(input_metadatas) -> str:
    """«Оценка по фактическому usage (gpt-image-2, тариф openai-pricing@…,
    курс 90,00 ₽/$ от 2026-09-20, cbr.ru)» from the ESTIMATED snapshots of
    the jobs; every distinct tariff/fx is named; "" without estimates."""
    seen = []
    for metadata in input_metadatas:
        cost = job_cost(metadata)
        if not is_estimated(cost):
            continue
        pricing = cost.get("pricing") or {}
        key = (pricing.get("model"), pricing.get("pricing_version"), pricing.get("fx_usd_rub"), pricing.get("fx_date"), pricing.get("fx_source"))
        if key not in seen:
            seen.append(key)
    if not seen:
        return ""
    parts = []
    for model, version, fx, fx_date, fx_source in seen:
        text = f"{model}, тариф {version}"
        if fx:
            text += f", курс {fx:.2f}".replace(".", ",") + " ₽/$"
            if fx_date:
                text += f" от {fx_date}"
            if fx_source:
                text += f", {fx_source}"
        else:
            text += ", курс не задан — только USD"
        parts.append(text)
    return "Оценка по фактическому usage (" + "; ".join(parts) + ")"
