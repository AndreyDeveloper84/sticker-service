"""Async C-1 rework: a queued (PENDING) job is a committed billable call the
worker will make, so the Budget Guard must see it. started_at is set only by
the worker's claim; a PENDING job counts by created_at.

Repro (D's scratch test): PILOT_MAX_IMAGE_CALLS_PER_DAY=1, one PENDING job →
a second order's preview must be refused, not accepted.
"""

from django.test import override_settings
from django.utils import timezone

from apps.core.models import GenerationJob, Order
from apps.core.services.budget import BudgetExceeded, BudgetGuard, BudgetService
from apps.core.services.generation import GenerationService
from apps.core.services.generation_cost import cost_snapshot
from apps.core.services.generation_queue import RecordingExecutor, use_executor
from apps.core.tests_pilot_budget import UNLIMITED, BudgetFixture, StickerProvider


@override_settings(**UNLIMITED)
class PendingJobsCountAgainstBudgetTests(BudgetFixture):
    def _pending(self, order, *, created_at=None, task=GenerationJob.TaskType.FULL, slot_key="e0"):
        job = GenerationJob.objects.create(
            order=order, task_type=task, status=GenerationJob.Status.PENDING,
            attempt=GenerationJob.objects.filter(order=order, task_type=task).count() + 1,
            provider="fake", slot_key=slot_key,
            input_metadata={"cost": cost_snapshot(provider=StickerProvider(), task_type=task)},
        )
        if created_at is not None:
            GenerationJob.objects.filter(pk=job.pk).update(created_at=created_at)
            job.refresh_from_db()
        self.assertIsNone(job.started_at)
        return job

    def test_pending_counts_in_day_month_and_order_counters(self):
        service = BudgetService()
        base = (service.calls_today(), service.calls_this_month(), service.calls_for_order(self.order))
        self._pending(self.order)
        self.assertEqual(
            (service.calls_today(), service.calls_this_month(), service.calls_for_order(self.order)),
            tuple(value + 1 for value in base),
        )

    def test_pending_from_yesterday_does_not_count_for_today_but_for_the_month(self):
        service = BudgetService()
        today, month = service.calls_today(), service.calls_this_month()
        yesterday = timezone.now() - timezone.timedelta(days=1)
        self._pending(self.order, created_at=yesterday)
        self.assertEqual(service.calls_today(), today)
        same_month = timezone.localtime(yesterday).month == timezone.localtime(timezone.now()).month
        self.assertEqual(service.calls_this_month(), month + (1 if same_month else 0))

    def test_pending_is_visible_in_order_costs_and_summary(self):
        before = BudgetService().order_costs(self.order)
        self._pending(self.order)
        after = BudgetService().order_costs(self.order)
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertEqual(after["full"], before["full"] + 1)
        # the snapshot is priced (CONFIG_SNAPSHOT) but not yet billable → possibly billable
        self.assertEqual(after["cost"]["possibly_billable_count"], before["cost"]["possibly_billable_count"] + 1)
        summary = BudgetService().summary()
        self.assertEqual(summary["today"]["used"], BudgetService().calls_today())
        self.assertGreaterEqual(summary["today"]["cost"]["possibly_billable_count"], 1)

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_day_limit_sees_the_queued_job(self):
        # fixture preview (started) + one PENDING = 2/2 → the next call is refused
        self._pending(self.order)
        with self.assertRaises(BudgetExceeded) as ctx:
            BudgetGuard().enforce(self.order, GenerationJob.TaskType.FULL, slot_key="e1", action="full_start")
        self.assertEqual(ctx.exception.decision.blocked.key, "day")
        self.assertEqual(ctx.exception.decision.blocked.used, 2)

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_repro_second_order_refused_while_first_is_only_queued(self):
        """D's scratch scenario through the real request path: the first
        order's preview is queued (worker not running), the second order's
        preview must be refused by the day limit — not accepted."""
        first = self._make_order(self.identity.user, self.order.product, self.order.style, status=Order.Status.PAID)
        second = self._make_order(self.identity.user, self.order.product, self.order.style, status=Order.Status.PAID)
        # the two fixture previews already count 2 today → clear them for a clean 1-limit picture
        GenerationJob.objects.filter(order__in=[first, second, self.order], status=GenerationJob.Status.SUCCEEDED).update(
            started_at=timezone.now() - timezone.timedelta(days=2)
        )
        service = GenerationService(provider=StickerProvider(), storage=self.storage)
        with use_executor(RecordingExecutor()) as executor, override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=1):
            queued = service.request_preview(order=first)
            self.assertEqual(queued.status, GenerationJob.Status.PENDING)
            self.assertEqual(executor.dispatched, [queued.pk])
            with self.assertRaises(BudgetExceeded):
                service.request_preview(order=second)
            self.assertEqual(executor.dispatched, [queued.pk])
        self.assertFalse(GenerationJob.objects.filter(order=second, status=GenerationJob.Status.PENDING).exists())
        second.refresh_from_db()
        self.assertEqual(second.status, Order.Status.PAID)  # rolled back with the refused job

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=2)
    def test_order_limit_sees_the_queued_job(self):
        self._pending(self.order)  # + fixture preview = 2/2
        with self.assertRaises(BudgetExceeded) as ctx:
            BudgetGuard().enforce(self.order, GenerationJob.TaskType.FULL, slot_key="e1", action="full_start")
        self.assertEqual(ctx.exception.decision.blocked.key, "order")
