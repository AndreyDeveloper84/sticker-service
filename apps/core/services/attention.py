"""Operator Attention Queue (DRF-2167): every live order that needs the
operator — or is waiting on something — sorted into one bucket each, with
its age and the next step. Read-only: no service is called, nothing changes.

Buckets (first match wins, most urgent first):

- recovery_required  — a PENDING job no worker picked up for 5 min, a RUNNING
                       job older than the stale limit, or the latest attempt
                       failed as ambiguous / queue_lost (blocked until an
                       explicit operator action);
- error              — the latest attempt FAILED (retryable / moderation /
                       api) and nothing is running: start it again;
- queued             — a PENDING attempt waits for the worker;
- generating         — a RUNNING attempt (age = seconds since it started);
- paid_idle          — PAID without any attempt for PAID_IDLE_AFTER;
- operator_action    — the order waits for the operator: internal preview
                       review, production to continue, final set to send;
- revision_requested — the customer asked for the included revision;
- waiting_customer   — the preview is with the customer;
- waiting_qc         — the set is ready for the QC checklist;
- partial_delivery   — a delivery run left failed / unsent slots or the
                       summary message.

Queries: 1 (orders + select_related) + 4 prefetches (generation_jobs,
events, final_deliveries, revision via select_related) — fixed, independent
of the number of orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Prefetch
from django.utils import timezone

from apps.core.console_text import JOB_TASKS, label
from apps.core.models import FinalDelivery, GenerationJob, Order, OrderEvent
from apps.core.services.generation import STALE_RUNNING_AFTER
from apps.core.services.generation_queue import QUEUE_LOST, QUEUE_WAIT_WARN_AFTER, WORKER_KEY

# PAID with no attempt for this long is "forgotten", not "just paid".
PAID_IDLE_AFTER = timedelta(minutes=15)

# Statuses that can never need attention.
TERMINAL = {Order.Status.DELIVERED, Order.Status.CANCELLED, Order.Status.FAILED}
# Statuses before payment: the customer is still in the bot.
PRE_PAYMENT = {
    Order.Status.DRAFT,
    Order.Status.AWAITING_PHOTOS,
    Order.Status.READY_FOR_CHECKOUT,
    Order.Status.AWAITING_PAYMENT,
}

BUCKETS = (
    ("recovery_required", "Нужно восстановление"),
    ("error", "Ошибка генерации"),
    ("queued", "В очереди"),
    ("generating", "Генерируется"),
    ("paid_idle", "Оплачен, генерация не запущена"),
    ("operator_action", "Ждёт оператора"),
    ("revision_requested", "Клиент запросил правку"),
    ("waiting_customer", "Ждём клиента"),
    ("waiting_qc", "Ждёт контроля качества"),
    ("partial_delivery", "Доставка не завершена"),
)
BUCKET_TITLES = dict(BUCKETS)


@dataclass(frozen=True)
class AttentionItem:
    order: Order
    bucket: str
    age: timedelta  # how long the order has been in this situation
    next_step: str  # what the operator does now, one sentence
    detail: str = ""  # the fact behind the bucket (job #, slot, failure)

    @property
    def title(self) -> str:
        return BUCKET_TITLES[self.bucket]


def age_text(age: timedelta) -> str:
    seconds = int(age.total_seconds())
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч {minutes % 60} мин"
    return f"{hours // 24} дн"


class AttentionQueue:
    def __init__(self, *, now=None):
        self.now = now or timezone.now()

    # ------------------------------------------------------------ data

    def orders(self):
        return (
            Order.objects.exclude(status__in=TERMINAL | PRE_PAYMENT)
            .select_related("product", "style", "channel_identity", "revision")
            .prefetch_related(
                Prefetch("generation_jobs", queryset=GenerationJob.objects.order_by("pk")),
                Prefetch(
                    "events",
                    queryset=OrderEvent.objects.filter(event_type=OrderEvent.Type.STATUS_CHANGED).order_by("pk"),
                ),
                Prefetch("final_deliveries", queryset=FinalDelivery.objects.order_by("attempt")),
            )
            .order_by("pk")
        )

    def build(self) -> dict[str, list[AttentionItem]]:
        buckets = {key: [] for key, _title in BUCKETS}
        for order in self.orders():
            item = self.classify(order)
            if item is not None:
                buckets[item.bucket].append(item)
        for items in buckets.values():
            items.sort(key=lambda item: -item.age.total_seconds())
        return buckets

    # -------------------------------------------------------- classify

    def status_age(self, order) -> timedelta:
        """Time since the order entered its current status (the last
        status_changed event to it), else since the last update."""
        since = order.updated_at
        for event in order.events.all():
            if event.to_status == order.status:
                since = event.created_at
        return self.now - since

    def classify(self, order) -> AttentionItem | None:
        jobs = list(order.generation_jobs.all())
        latest = jobs[-1] if jobs else None
        pending = [job for job in jobs if job.status == GenerationJob.Status.PENDING]
        running = [job for job in jobs if job.status == GenerationJob.Status.RUNNING]

        # --- recovery: queue lost / stale worker / blocked attempts
        for job in pending:
            waited = self.now - job.created_at
            picked = ((job.output_metadata or {}).get(WORKER_KEY) or {}).get("picked_at")
            if waited >= QUEUE_WAIT_WARN_AFTER and not picked:
                return AttentionItem(
                    order, "recovery_required", waited,
                    "Worker не берёт задачу: проверьте индикатор worker'а, при необходимости «Снять из очереди».",
                    f"job #{job.pk} ({self._job_title(job)}) ждёт worker'а",
                )
        for job in running:
            started = job.started_at or job.created_at
            if started and self.now - started >= STALE_RUNNING_AFTER:
                return AttentionItem(
                    order, "recovery_required", self.now - started,
                    "Worker не ответил: откройте карточку — попытка будет помечена как неоднозначная.",
                    f"job #{job.pk} ({self._job_title(job)}) висит в RUNNING",
                )
        if latest is not None and latest.status == GenerationJob.Status.FAILED and not pending and not running:
            failure_class = (latest.output_metadata or {}).get("failure_class")
            if failure_class in ("ambiguous", QUEUE_LOST):
                what = "неоднозначно (возможно, оплачено)" if failure_class == "ambiguous" else "снята из очереди"
                step = (
                    "Слот заблокирован — «Принудительный повтор» после ручной проверки."
                    if latest.task_type == GenerationJob.TaskType.FULL and failure_class == "ambiguous"
                    else "Запустите генерацию заново."
                )
                return AttentionItem(
                    order, "recovery_required", self.now - (latest.finished_at or latest.updated_at),
                    step, f"job #{latest.pk} ({self._job_title(latest)}) — {what}",
                )
            # --- plain error: start again
            error = (latest.error or "")[:120]
            return AttentionItem(
                order, "error", self.now - (latest.finished_at or latest.updated_at),
                "Повторите генерацию (превью / слот) или запросите у клиента другое фото.",
                f"job #{latest.pk} ({self._job_title(latest)}) — ошибка: {error or failure_class or 'без описания'}",
            )

        # --- the worker is on it
        if pending:
            job = pending[0]
            return AttentionItem(
                order, "queued", self.now - job.created_at,
                "Ничего не делать — worker возьмёт задачу.", f"job #{job.pk} ({self._job_title(job)})",
            )
        if running:
            job = running[0]
            started = job.started_at or job.created_at
            return AttentionItem(
                order, "generating", self.now - started,
                "Ничего не делать — результат появится в карточке.", f"job #{job.pk} ({self._job_title(job)})",
            )

        status = order.status
        age = self.status_age(order)

        if status == Order.Status.PAID:
            if age < PAID_IDLE_AFTER:
                return None  # just paid: the operator has a moment
            return AttentionItem(order, "paid_idle", age, "Нажмите «Сгенерировать превью».")
        if status == Order.Status.PREVIEW_GENERATING:
            # no attempt at all in this status: the job creation never happened
            return AttentionItem(order, "paid_idle", age, "Нажмите «Сгенерировать превью».")
        if status == Order.Status.INTERNAL_PREVIEW_REVIEW:
            return AttentionItem(order, "operator_action", age, "Проверьте превью, одобрите и отправьте клиенту.")
        if status == Order.Status.PREVIEW_REVIEW:
            return AttentionItem(
                order, "waiting_customer", age,
                "Ждём «Нравится» или правку; при долгом молчании напомните клиенту.",
            )
        if status in (Order.Status.REVISION_REQUESTED, Order.Status.REVISION_GENERATING):
            category = ""
            try:
                category = order.revision.category
            except Order.revision.RelatedObjectDoesNotExist:
                pass
            return AttentionItem(
                order, "revision_requested", age, "Нажмите «Сгенерировать правку».",
                f"категория: {category}" if category else "",
            )
        if status == Order.Status.PACK_GENERATING:
            return AttentionItem(
                order, "operator_action", age,
                "Продолжите производство («Запустить производство» / «Повторить неудавшиеся слоты»).",
            )
        if status == Order.Status.QUALITY_CONTROL:
            return AttentionItem(order, "waiting_qc", age, "Откройте QC-отчёт и заполните чек-лист.")
        if status == Order.Status.READY_FOR_DELIVERY:
            return AttentionItem(order, "operator_action", age, "Нажмите «Отправить набор клиенту».")
        if status == Order.Status.DELIVERY_IN_PROGRESS:
            return AttentionItem(
                order, "partial_delivery", age,
                "Нажмите «Продолжить доставку» — уйдут только неотправленные слоты.",
                self._delivery_detail(order),
            )
        return None

    # --------------------------------------------------------- helpers

    @staticmethod
    def _job_title(job) -> str:
        text = label(JOB_TASKS, job.task_type)
        if job.slot_key:
            text += f" «{job.slot_key}»"
        return text

    @staticmethod
    def _delivery_detail(order) -> str:
        runs = list(order.final_deliveries.all())
        if not runs:
            return "доставка ещё не начиналась"
        sent, failed = set(), {}
        for run in runs:
            for result in run.results or []:
                key = result.get("slot_key")
                if result.get("status") == "sent":
                    sent.add(key)
                    failed.pop(key, None)
                elif key not in sent:
                    failed[key] = result.get("failure_class") or "failed"
        summary = runs[-1].summary or {}
        parts = [f"попыток: {len(runs)}", f"отправлено слотов: {len(sent)}"]
        if failed:
            parts.append("не ушли: " + ", ".join(f"{key} ({cls})" for key, cls in failed.items()))
        if summary.get("status") == "failed":
            parts.append("итоговое сообщение не ушло")
        return " · ".join(parts)
