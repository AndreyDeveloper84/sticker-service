"""Async C-2: FULL production as a lazy chain through the executor.

One request → one PENDING slot job; the worker (run_job) executes it and,
after a success, creates + dispatches the next slot under the order lock.
Never more than one PENDING/RUNNING slot per order; any failed slot stops the
chain; the whole remaining pack is checked against the budget upfront.
"""

from unittest import mock

from apps.core.models import GenerationJob, Order
from apps.core.services.full_production import FullProductionError
from apps.core.services.generation_queue import RecordingExecutor, claim, run_job, use_executor
from apps.core.tests_full_generation import (
    ClassifyingProvider,
    FakeProvider,
    FullProductionTestCase,
)


class LazyChainTests(FullProductionTestCase):
    def _active(self, order):
        return self._full_jobs(order).filter(
            status__in=[GenerationJob.Status.PENDING, GenerationJob.Status.RUNNING]
        )

    def test_request_creates_one_pending_slot_and_dispatches_once(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        service = self._service(provider)
        with use_executor(RecordingExecutor()) as executor:
            plan = service.start(order=order, max_slots=None)
            self.assertEqual(self._full_jobs(order).count(), 1)
            head = self._full_jobs(order).get()
            self.assertEqual(head.status, GenerationJob.Status.PENDING)
            self.assertEqual(head.slot_key, "hello")
            self.assertIsNone(head.started_at)
            self.assertEqual(executor.dispatched, [head.pk])
            self.assertEqual(provider.requests, [])  # no provider call in the request
            self.assertEqual([s.status for s in plan], ["queued", "pending", "pending"])
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PACK_GENERATING)
            chain = head.input_metadata["chain"]
            self.assertEqual(chain, {"mode": "start", "requested_slots": None, "remaining": None, "auto_continue": True})

    def test_chain_continues_slot_by_slot_never_two_active(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        service = self._service(provider)
        with use_executor(RecordingExecutor()) as executor:
            service.start(order=order, max_slots=None)
            for expected_done in (1, 2, 3):
                self.assertEqual(len(executor.dispatched), 1)
                self.assertLessEqual(self._active(order).count(), 1)
                executor.run_all(service=service)
                self.assertEqual(len(provider.requests), expected_done)
                self.assertEqual(self._final_assets(order).count(), expected_done)
            self.assertEqual(executor.dispatched, [])  # chain finished, nothing queued
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        jobs = list(self._full_jobs(order).order_by("attempt"))
        self.assertEqual([j.slot_key for j in jobs], ["hello", "bye", "thanks"])
        self.assertTrue(all(j.status == GenerationJob.Status.SUCCEEDED for j in jobs))
        self.assertTrue(all(j.output_metadata["worker"]["finished_at"] for j in jobs))

    def test_chain_stops_on_failure_and_leaves_rest_pending(self):
        order, _preview = self._make_order()
        provider = ClassifyingProvider({"bye": "ambiguous"})
        service = self._service(provider)
        with use_executor(RecordingExecutor()) as executor:
            service.start(order=order, max_slots=None)
            executor.run_all(service=service)  # hello ok → bye queued
            executor.run_all(service=service)  # bye ambiguous → stop
            self.assertEqual(executor.dispatched, [])
        self.assertEqual(self._active(order).count(), 0)
        self.assertEqual(self._full_jobs(order).filter(slot_key="thanks").count(), 0)
        plan = service.production_plan(order)
        self.assertEqual([s.status for s in plan], ["succeeded", "failed", "pending"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)

    def test_reentry_while_slot_queued_is_refused(self):
        order, _preview = self._make_order()
        service = self._service(FakeProvider())
        with use_executor(RecordingExecutor()):
            service.start(order=order, max_slots=None)
            with self.assertRaises(FullProductionError) as ctx:
                service.start(order=order, max_slots=None)
            self.assertIn("в очередь", str(ctx.exception))
            self.assertEqual(self._full_jobs(order).count(), 1)

    def test_run_job_redelivery_is_a_noop(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        service = self._service(provider)
        with use_executor(RecordingExecutor()) as executor:
            service.start(order=order, max_slots=None)
            head = executor.dispatched[0]
            run_job(head, service=service)
            run_job(head, service=service)  # duplicate message
        self.assertEqual(len([r for r in provider.requests if r.metadata["slot_key"] == "hello"]), 1)

    def test_budget_checked_for_the_whole_pack_upfront(self):
        order, _preview = self._make_order()
        service = self._service(FakeProvider())
        calls = []

        def fake_enforce(self_, locked, task_type, *, slot_key="", action=None, planned=1):
            calls.append((slot_key, action, planned))

        with mock.patch("apps.core.services.full_production.BudgetGuard.enforce", fake_enforce):
            with use_executor(RecordingExecutor()):
                service.start(order=order, max_slots=None)
        # pack of 3: planned=3 without slot, then the head slot on its own
        self.assertEqual(calls[0], ("", "full_start", 3))
        self.assertEqual(calls[1], ("hello", "full_start", 1))

    def test_budget_refusal_for_the_pack_creates_nothing(self):
        order, _preview = self._make_order()
        service = self._service(FakeProvider())
        with mock.patch(
            "apps.core.services.full_production.BudgetGuard.enforce",
            side_effect=FullProductionError("budget: pack of 3 exceeds the day limit"),
        ):
            with use_executor(RecordingExecutor()) as executor:
                with self.assertRaises(FullProductionError):
                    service.start(order=order, max_slots=None)
                self.assertEqual(executor.dispatched, [])
        self.assertEqual(self._full_jobs(order).count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)  # rolled back with the job

    def test_force_retry_is_a_single_slot_without_chain(self):
        order, _preview = self._make_order()
        self._service(ClassifyingProvider({"bye": "ambiguous"})).start(order=order, max_slots=None)
        service = self._service(FakeProvider())
        with use_executor(RecordingExecutor()) as executor:
            service.force_retry_slot(order=order, slot_key="bye")
            head = self._full_jobs(order).get(pk=executor.dispatched[0])
            self.assertFalse(head.input_metadata["chain"]["auto_continue"])
            executor.run_all(service=service)
            self.assertEqual(executor.dispatched, [])  # thanks is not started by force
        self.assertEqual(self._full_jobs(order).filter(slot_key="thanks").count(), 0)

    def test_stale_running_slot_reaped_then_claim_skips(self):
        order, _preview = self._make_order()
        service = self._service(FakeProvider())
        with use_executor(RecordingExecutor()) as executor:
            service.start(order=order, max_slots=None)
            head = executor.dispatched[0]
        claim(head)
        from datetime import timedelta

        from django.utils import timezone

        GenerationJob.objects.filter(pk=head).update(started_at=timezone.now() - timedelta(minutes=16))
        run_job(head, service=service)  # reaps, then finds non-PENDING → skip
        job = GenerationJob.objects.get(pk=head)
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], "ambiguous")
        self.assertEqual(service.provider.requests, [])
