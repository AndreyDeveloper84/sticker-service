"""Pilot Budget Guard (DRF-2086): limits enforced at the service boundary.

Every billable path — preview, revision, FULL slot, retry, regenerate, force
retry, and any future caller — creates its ``GenerationJob`` under
``select_for_update(order)`` before the provider is called. The guard runs
inside that transaction, right before the job row is written:

    BudgetGuard(override=...).enforce(locked_order, task_type, slot_key=...)

- limit exceeded → ``BudgetExceeded`` (Russian message) → the transaction
  rolls back → no job, no provider call;
- ``BudgetOverride`` (explicit, from a superuser who confirmed) →
  ``OrderEvent budget.override`` is written in the same transaction and the
  action proceeds; without it everyone is blocked;
- invalid configuration → ``BudgetConfigError`` (fail closed, RU message
  naming the variable).

Limits (Django setting first, then env):

- ``PILOT_MAX_IMAGE_CALLS_PER_DAY`` / ``PILOT_MAX_IMAGE_CALLS_PER_MONTH`` —
  jobs with ``started_at`` in the current local day / month, every task
  type and outcome (an attempt that reached the provider is billable),
  PLUS queued (PENDING) jobs by ``created_at`` — a queued attempt is a
  committed billable call the worker will make (async C-1); unset or
  ``0`` = unlimited;
- ``PILOT_MAX_IMAGE_CALLS_PER_ORDER`` (unset → 15; ``0`` = unlimited);
- ``PILOT_MAX_FULL_ATTEMPTS_PER_SLOT`` (unset → 3; ``0`` = unlimited) —
  FULL jobs per slot.

Per-order / per-slot counters are read under the order lock (concurrency
safe). Day/month counters span orders: on PostgreSQL the guard takes a
transaction-scoped advisory lock (``BUDGET_LOCK_KEY``) before counting so
two concurrent transactions cannot both see the last free unit; on other
backends they are best-effort.

``PILOT_IMAGE_CALL_COST_RUB`` (optional float) is the tariff snapshotted
into every new job at attempt time (DRF-2111, ``generation_cost``); the
operator-facing cost figures are sums of those snapshots — never
"today's price × calls".
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.models import GenerationJob, Order, OrderEvent

LIMIT_TITLES = {
    "day": "вызовов в день",
    "month": "вызовов в месяц",
    "order": "вызовов на заказ",
    "slot": "попыток на слот",
}

DEFAULTS = {
    "PILOT_MAX_IMAGE_CALLS_PER_DAY": 0,
    "PILOT_MAX_IMAGE_CALLS_PER_MONTH": 0,
    "PILOT_MAX_IMAGE_CALLS_PER_ORDER": 15,
    "PILOT_MAX_FULL_ATTEMPTS_PER_SLOT": 3,
}

# PostgreSQL advisory-lock key (bigint) used to serialize the cross-order
# day/month counters while a job is being created. Transaction-scoped
# (pg_advisory_xact_lock): released automatically at commit or rollback, no
# schema, no row. Any constant works as long as nothing else in the database
# uses the same key; "2086" = the ticket, "0001" = the budget counters.
BUDGET_LOCK_KEY = 20860001

TASK_ACTIONS = {
    GenerationJob.TaskType.PREVIEW: "preview",
    GenerationJob.TaskType.REVISION: "revision",
    GenerationJob.TaskType.FULL: "full",
}


class BudgetError(ValueError):
    """Base: the action must not reach the provider."""


class BudgetConfigError(BudgetError):
    """A PILOT_MAX_* value is not a non-negative integer — fail closed."""


class BudgetExceeded(BudgetError):
    def __init__(self, decision: "BudgetDecision"):
        self.decision = decision
        super().__init__(decision.message)


@dataclass(frozen=True)
class BudgetOverride:
    """Explicit, audited permission to exceed a limit for one action."""

    actor_ref: str
    reason: str = ""


# ------------------------------------------------------------- settings


def setting(name: str, default=None):
    """Django setting first (tests), then the environment, then default."""
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _config_message(name: str, raw) -> str:
    return (
        f"Некорректная настройка лимита {name}: значение {raw!r} не является целым числом ≥ 0. "
        "Генерация не запущена."
    )


def limit_value(name: str) -> int | None:
    """Positive int limit or None (= unlimited).

    Unset → DEFAULTS (day/month unlimited, order 15, slot 3); explicit 0 →
    unlimited; anything else that is not a non-negative integer →
    BudgetConfigError (fail closed).
    """
    raw = setting(name, DEFAULTS[name])
    if isinstance(raw, bool):
        raise BudgetConfigError(_config_message(name, raw))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise BudgetConfigError(_config_message(name, raw)) from None
    if value < 0:
        raise BudgetConfigError(_config_message(name, raw))
    return value if value > 0 else None


def safe_limit(name: str) -> int | None:
    """Display helper: an invalid value shows as None instead of raising."""
    try:
        return limit_value(name)
    except BudgetConfigError:
        return None


def call_cost_rub() -> float | None:
    raw = setting("PILOT_IMAGE_CALL_COST_RUB")
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def percent(used: int, maximum: int | None) -> int | None:
    if not maximum:
        return None
    return int(round(100 * used / maximum))


# ------------------------------------------------------------- decision


@dataclass(frozen=True)
class BudgetLimit:
    key: str  # day | month | order | slot
    used: int
    max: int
    planned: int = 1
    slot_key: str = ""

    @property
    def title(self) -> str:
        return LIMIT_TITLES[self.key]

    @property
    def exceeded(self) -> bool:
        return self.used + self.planned > self.max


@dataclass(frozen=True)
class BudgetDecision:
    action: str
    limits: list[BudgetLimit] = field(default_factory=list)

    @property
    def blocked(self) -> BudgetLimit | None:
        for limit in self.limits:
            if limit.exceeded:
                return limit
        return None

    @property
    def message(self) -> str:
        limit = self.blocked
        if limit is None:
            return ""
        where = f" ({limit.slot_key})" if limit.slot_key else ""
        return f"Лимит {limit.title}{where} исчерпан: {limit.used}/{limit.max}. Генерация не запущена."


def _event_payload(decision: BudgetDecision, **extra) -> dict:
    limit = decision.blocked
    return {
        "action": decision.action,
        "limit": limit.key,
        "slot_key": limit.slot_key,
        "used": limit.used,
        "max": limit.max,
        **extra,
    }


# -------------------------------------------------------------- service


class BudgetService:
    """Usage counters, limit checks, evidence and cost figures."""

    def __init__(self, *, now: datetime | None = None):
        self.now = now or timezone.now()

    # windows

    def day_window(self) -> tuple[datetime, datetime]:
        local = timezone.localtime(self.now)
        start = timezone.make_aware(datetime.combine(local.date(), time.min), local.tzinfo)
        return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)

    def month_window(self) -> tuple[datetime, datetime]:
        local = timezone.localtime(self.now)
        start = timezone.make_aware(datetime.combine(local.date().replace(day=1), time.min), local.tzinfo)
        next_month = (start.replace(day=28) + timezone.timedelta(days=4)).replace(day=1)
        return start, next_month

    # counters

    @staticmethod
    def _started():
        """Attempts that count against the budget: everything that reached
        (or will reach) the provider — started jobs by ``started_at`` and
        queued PENDING jobs by ``created_at`` (async C-1: ``started_at`` is
        set by the worker's claim, so a PENDING job would otherwise be
        invisible and N queued jobs could overshoot a day limit; with the
        worker down, indefinitely). ``counted_at`` is the window key.

        Nuance: a job requested at 23:59 and claimed at 00:01 moves from
        today's window (as PENDING by created_at) to tomorrow's (as started
        by started_at). The limit is still enforced at request time against
        the window in which the request is made, which is the intent.
        """
        return GenerationJob.objects.filter(
            Q(started_at__isnull=False) | Q(status=GenerationJob.Status.PENDING)
        ).annotate(counted_at=Coalesce("started_at", "created_at"))

    def calls_today(self) -> int:
        start, end = self.day_window()
        return self._started().filter(counted_at__gte=start, counted_at__lte=end).count()

    def calls_this_month(self) -> int:
        start, end = self.month_window()
        return self._started().filter(counted_at__gte=start, counted_at__lt=end).count()

    def calls_for_order(self, order: Order) -> int:
        return self._started().filter(order=order).count()

    def full_attempts(self, order: Order) -> dict[str, int]:
        rows = (
            GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.FULL)
            .values("slot_key")
            .annotate(count=Count("pk"))
        )
        return {row["slot_key"]: row["count"] for row in rows}

    # check

    def check(self, order: Order, action: str, *, slot_keys=None, planned: int = 1) -> BudgetDecision:
        """Limits ``planned`` more calls would hit. ``slot_keys``: FULL slots
        the calls target (None on production start = slots without a FINAL
        asset; [] = no per-slot check). Raises BudgetConfigError on an
        invalid configuration."""
        limits: list[BudgetLimit] = []
        day = limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY")
        if day:
            limits.append(BudgetLimit("day", self.calls_today(), day, planned))
        month = limit_value("PILOT_MAX_IMAGE_CALLS_PER_MONTH")
        if month:
            limits.append(BudgetLimit("month", self.calls_this_month(), month, planned))
        per_order = limit_value("PILOT_MAX_IMAGE_CALLS_PER_ORDER")
        if per_order:
            limits.append(BudgetLimit("order", self.calls_for_order(order), per_order, planned))
        per_slot = limit_value("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT")
        if per_slot and action in {"full", "full_start", "retry", "regenerate", "force_retry"}:
            attempts = self.full_attempts(order)
            keys = list(slot_keys) if slot_keys is not None else self._open_slots(order)
            for key in keys:
                limits.append(BudgetLimit("slot", attempts.get(key, 0), per_slot, 1, slot_key=key))
        return BudgetDecision(action=action, limits=limits)

    @staticmethod
    def _open_slots(order: Order) -> list[str]:
        from apps.core.models import GeneratedAsset

        done = set(
            order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL).values_list("slot_key", flat=True)
        )
        return [key for key in (order.selection or {}).get("emotions") or [] if key not in done]

    # evidence

    @staticmethod
    def record_blocked(order: Order, decision: BudgetDecision, *, actor_ref: str = "") -> OrderEvent:
        return OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.BUDGET_BLOCKED,
            actor_kind=OrderEvent.Actor.OPERATOR,
            actor_ref=actor_ref,
            payload=_event_payload(decision),
        )

    @staticmethod
    def record_override(order: Order, decision: BudgetDecision, override: BudgetOverride) -> OrderEvent:
        return OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.BUDGET_OVERRIDE,
            actor_kind=OrderEvent.Actor.OPERATOR,
            actor_ref=override.actor_ref,
            payload=_event_payload(decision, reason=override.reason),
        )

    # costs

    def order_costs(self, order: Order) -> dict:
        """Calls, tokens and the cost evidence of one order.

        ``cost`` = generation_cost.aggregate() over the order's started jobs:
        a sum only of snapshots that are both priced (CONFIG_SNAPSHOT) and
        billable; unknown / possibly billable jobs are counted, never priced.
        """
        from apps.core.services import generation_cost

        jobs = list(self._started().filter(order=order).values("task_type", "input_metadata", "output_metadata"))
        by_task = Counter(job["task_type"] for job in jobs)
        tokens = 0
        for job in jobs:
            usage = (job["output_metadata"] or {}).get("usage") or {}
            value = usage.get("total_tokens")
            if isinstance(value, int):
                tokens += value
        attempts = self.full_attempts(order)
        return {
            "calls": len(jobs),
            "preview": by_task.get(GenerationJob.TaskType.PREVIEW, 0),
            "revision": by_task.get(GenerationJob.TaskType.REVISION, 0),
            "full": by_task.get(GenerationJob.TaskType.FULL, 0),
            "tokens": tokens,
            "cost": generation_cost.aggregate(job["input_metadata"] for job in jobs),
            "max_attempts_per_slot": max(attempts.values(), default=0),
            "slot_limit": safe_limit("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"),
            "order_limit": safe_limit("PILOT_MAX_IMAGE_CALLS_PER_ORDER"),
        }

    def _window_cost(self, start, end, *, inclusive_end: bool) -> dict:
        from apps.core.services import generation_cost

        jobs = self._started().filter(counted_at__gte=start)
        jobs = jobs.filter(counted_at__lte=end) if inclusive_end else jobs.filter(counted_at__lt=end)
        return generation_cost.aggregate(jobs.values_list("input_metadata", flat=True))

    def summary(self) -> dict:
        day_start, day_end = self.day_window()
        month_start, month_end = self.month_window()
        return {
            "today": {
                "used": self.calls_today(),
                "max": safe_limit("PILOT_MAX_IMAGE_CALLS_PER_DAY"),
                "cost": self._window_cost(day_start, day_end, inclusive_end=True),
            },
            "month": {
                "used": self.calls_this_month(),
                "max": safe_limit("PILOT_MAX_IMAGE_CALLS_PER_MONTH"),
                "cost": self._window_cost(month_start, month_end, inclusive_end=False),
            },
            "cost_rub_per_call": call_cost_rub(),
        }


# ---------------------------------------------------------------- guard


def serialize_budget_counters() -> None:
    """Take the transaction-scoped advisory lock on PostgreSQL (released at
    commit/rollback) so concurrent job creations count the day/month usage
    one after another. No-op on other backends (best-effort)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BUDGET_LOCK_KEY])


class BudgetGuard:
    """The single enforcement point. Call inside the job-creating transaction
    (order already locked) right before the ``GenerationJob`` row is
    written; ``planned`` = jobs already created by the same call + 1."""

    def __init__(self, *, override: BudgetOverride | None = None):
        self.override = override

    def enforce(
        self,
        locked_order: Order,
        task_type: str,
        *,
        slot_key: str = "",
        action: str | None = None,
        planned: int = 1,
    ) -> BudgetDecision:
        serialize_budget_counters()
        action = action or TASK_ACTIONS.get(task_type, str(task_type))
        if task_type == GenerationJob.TaskType.FULL:
            slot_keys = [slot_key] if slot_key else []
        else:
            slot_keys = []
        decision = BudgetService().check(locked_order, action, slot_keys=slot_keys, planned=planned)
        if decision.blocked is None:
            return decision
        if self.override is None:
            raise BudgetExceeded(decision)
        BudgetService.record_override(locked_order, decision, self.override)
        return decision
