"""Pilot budget control (DRF-2086): limits, cost figures and their evidence.

Console-level only. Generation services are not touched: the Production
Console asks ``BudgetService.check()`` before every paid action and refuses
to call the service when a limit would be exceeded; a superuser may
override one action explicitly, and both outcomes are recorded as
``OrderEvent`` rows (``budget.blocked`` / ``budget.override``).

Limits come from settings/env (settings take precedence so tests can use
``override_settings``):

- ``PILOT_MAX_IMAGE_CALLS_PER_DAY``, ``PILOT_MAX_IMAGE_CALLS_PER_MONTH`` —
  provider calls (every ``GenerationJob`` that started, all task types) in
  the current local day / month; absent or 0 = unlimited;
- ``PILOT_MAX_FULL_ATTEMPTS_PER_SLOT`` (default 3) — FULL jobs per slot;
- ``PILOT_MAX_IMAGE_CALLS_PER_ORDER`` (default 15) — calls per order;
  explicit 0 = unlimited for these two as well.

``PILOT_IMAGE_CALL_COST_RUB`` (optional float) prices one call for the
operator-facing «≈ ₽» figures; ``PILOT_IMAGE_CALL_COST_USD`` stays the
snapshot's unit.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.core.models import GenerationJob, Order, OrderEvent

# Provider calls a console action will make in ONE click. Every production
# action runs a single slot per click (max_slots=1), so all actions cost 1.
ACTION_CALLS = {
    "preview": 1,
    "revision": 1,
    "full_start": 1,
    "retry": 1,
    "regenerate": 1,
    "force_retry": 1,
}

ACTION_TITLES = {
    "preview": "Сгенерировать превью",
    "revision": "Сгенерировать правку",
    "full_start": "Запустить производство",
    "retry": "Повторить неудавшиеся слоты",
    "regenerate": "Перегенерировать стикеры",
    "force_retry": "Принудительный повтор слота",
}

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


def setting(name: str, default=None):
    """Django setting first (tests), then the environment, then default."""
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def limit_value(name: str) -> int | None:
    """Positive int limit or None (= unlimited) for a PILOT_MAX_* setting."""
    raw = setting(name, DEFAULTS[name])
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def call_cost_rub() -> float | None:
    raw = setting("PILOT_IMAGE_CALL_COST_RUB")
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


def rub(calls: int) -> float | None:
    cost = call_cost_rub()
    return round(calls * cost, 2) if cost is not None else None


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

    @property
    def percent(self) -> int:
        return int(round(100 * self.used / self.max)) if self.max else 0


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
        return (
            f"Лимит {limit.title}{where} исчерпан: {limit.used}/{limit.max}. "
            "Генерация не запущена."
        )


class BudgetService:
    """Usage counters, limit checks and cost figures for the console."""

    def __init__(self, *, now: datetime | None = None):
        self.now = now or timezone.now()

    # ---------------------------------------------------------- windows

    def day_window(self) -> tuple[datetime, datetime]:
        local = timezone.localtime(self.now)
        start = timezone.make_aware(datetime.combine(local.date(), time.min), local.tzinfo)
        return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)

    def month_window(self) -> tuple[datetime, datetime]:
        local = timezone.localtime(self.now)
        start = timezone.make_aware(datetime.combine(local.date().replace(day=1), time.min), local.tzinfo)
        next_month = (start.replace(day=28) + timezone.timedelta(days=4)).replace(day=1)
        return start, next_month

    # ---------------------------------------------------------- counters

    @staticmethod
    def _started():
        return GenerationJob.objects.filter(started_at__isnull=False)

    def calls_today(self) -> int:
        start, end = self.day_window()
        return self._started().filter(started_at__gte=start, started_at__lte=end).count()

    def calls_this_month(self) -> int:
        start, end = self.month_window()
        return self._started().filter(started_at__gte=start, started_at__lt=end).count()

    def calls_for_order(self, order: Order) -> int:
        return self._started().filter(order=order).count()

    def full_attempts(self, order: Order) -> dict[str, int]:
        rows = (
            GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.FULL)
            .values("slot_key")
            .annotate(count=Count("pk"))
        )
        return {row["slot_key"]: row["count"] for row in rows}

    # ------------------------------------------------------------- check

    def check(self, order: Order, action: str, *, slot_keys=None) -> BudgetDecision:
        """Limits this action would hit. ``slot_keys``: slots the action may
        run (explicit for regenerate/force_retry/retry; production start
        defaults to the slots without a final asset)."""
        planned = ACTION_CALLS.get(action, 1)
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
        if per_slot and action in {"full_start", "retry", "regenerate", "force_retry"}:
            attempts = self.full_attempts(order)
            keys = list(slot_keys) if slot_keys is not None else self._open_slots(order)
            for key in keys:
                limits.append(BudgetLimit("slot", attempts.get(key, 0), per_slot, 1, slot_key=key))
        return BudgetDecision(action=action, limits=limits)

    @staticmethod
    def _open_slots(order: Order) -> list[str]:
        """Slots production may still run: selected emotions without a FINAL asset."""
        from apps.core.models import GeneratedAsset

        done = set(
            order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL).values_list("slot_key", flat=True)
        )
        return [key for key in (order.selection or {}).get("emotions") or [] if key not in done]

    # ---------------------------------------------------------- evidence

    @staticmethod
    def record_blocked(order: Order, decision: BudgetDecision, *, actor_ref: str = "") -> OrderEvent:
        limit = decision.blocked
        return OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.BUDGET_BLOCKED,
            actor_kind=OrderEvent.Actor.OPERATOR,
            actor_ref=actor_ref,
            payload={
                "action": decision.action,
                "limit": limit.key,
                "slot_key": limit.slot_key,
                "used": limit.used,
                "max": limit.max,
            },
        )

    @staticmethod
    def record_override(order: Order, decision: BudgetDecision, *, actor_ref: str = "") -> OrderEvent:
        limit = decision.blocked
        return OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.BUDGET_OVERRIDE,
            actor_kind=OrderEvent.Actor.OPERATOR,
            actor_ref=actor_ref,
            payload={
                "action": decision.action,
                "limit": limit.key,
                "slot_key": limit.slot_key,
                "used": limit.used,
                "max": limit.max,
            },
        )

    # ------------------------------------------------------------- costs

    def order_costs(self, order: Order) -> dict:
        jobs = list(self._started().filter(order=order).values("task_type", "output_metadata"))
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
            "rub": rub(len(jobs)),
            "max_attempts_per_slot": max(attempts.values(), default=0),
            "slot_limit": limit_value("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"),
            "order_limit": limit_value("PILOT_MAX_IMAGE_CALLS_PER_ORDER"),
        }

    def summary(self) -> dict:
        today = self.calls_today()
        month = self.calls_this_month()
        return {
            "today": {"used": today, "max": limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY"), "rub": rub(today)},
            "month": {"used": month, "max": limit_value("PILOT_MAX_IMAGE_CALLS_PER_MONTH"), "rub": rub(month)},
            "cost_rub_per_call": call_cost_rub(),
        }


def percent(used: int, maximum: int | None) -> int | None:
    if not maximum:
        return None
    return int(round(100 * used / maximum))
