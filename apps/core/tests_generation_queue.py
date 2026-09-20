"""Async generation C-1 (design 2026-09-20): executor split, at-most-once claim,
reap_stale, dequeue, worker facts. Everything runs with the inline or the
recording executor — no Redis in the test suite.
"""

import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, OrderPhoto, Product, Style, User
from apps.core.services import generation_queue
from apps.core.services.generation import (
    ALREADY_RUNNING_MESSAGE,
    QUEUED_MESSAGE,
    GenerationError,
    GenerationService,
    reap_stale,
)
from apps.core.services.generation_cost import BillingOutcome, job_cost
from apps.core.services.generation_queue import (
    QUEUE_LOST,
    DequeueError,
    InlineExecutor,
    RecordingExecutor,
    RQExecutor,
    claim,
    dequeue,
    run_job,
    use_executor,
)
from apps.core.storage import LocalMediaStorage


class FakeProvider:
    name = "fake"

    def __init__(self, fail=None):
        self.fail = fail
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        return ImageGenerationResult(content=b"preview", metadata={"fake": True, "proxy": "proxy[0] p.example:3128"})


class QueueTestCase(TestCase):
    def setUp(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="max", external_user_id="q-user")
        product = Product.objects.create(code="stickers", name="Stickers", config={"generation_prompt": "Make preview"})
        style = Style.objects.create(code="3d", name="3D", config={"prompt": "3D style"})
        self.order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._settings = override_settings(MEDIA_ROOT=Path(self._tmp.name))
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.storage = LocalMediaStorage()
        key = f"orders/{self.order.pk}/reference.jpg"
        self.storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=key, original_filename="reference.jpg", mime_type="image/jpeg", size_bytes=12)

    def service(self, provider=None):
        return GenerationService(provider=provider or FakeProvider(), storage=self.storage)


class InlineExecutorTests(QueueTestCase):
    def test_default_executor_is_inline_and_runs_the_job_in_place(self):
        self.assertIsInstance(generation_queue.get_executor(), InlineExecutor)
        service = self.service()
        job = service.request_preview(order=self.order)
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(len(service.provider.requests), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        # queue / worker evidence
        self.assertEqual(job.input_metadata["queue"]["executor"], "inline")
        self.assertIn("requested_at", job.input_metadata["queue"])
        worker = job.output_metadata["worker"]
        self.assertTrue(worker["picked_at"] and worker["finished_at"])
        self.assertGreaterEqual(worker["duration_s"], 0)
        self.assertEqual(worker["proxy"], "proxy[0] p.example:3128")
        self.assertNotIn("proxy", job.output_metadata)  # moved under worker
        asset = GeneratedAsset.objects.get(pk=job.output_metadata["asset_id"])
        self.assertNotIn("proxy", asset.metadata)
        self.assertIsNotNone(job.started_at)
        self.assertEqual(job_cost(job.input_metadata)["billing_outcome"], BillingOutcome.SUCCESS)

    def test_sync_wrapper_keeps_asset_or_error_contract(self):
        asset = self.service().generate_preview(order=self.order)
        self.assertEqual(asset.kind, GeneratedAsset.Kind.PREVIEW)
        # failure → GenerationError with .failure facts (console humanize_error);
        # a restart from internal review opens attempt 2 (assets are PROTECT)
        self.order.refresh_from_db()
        with self.assertRaises(GenerationError) as ctx:
            self.service(FakeProvider(fail=RuntimeError("provider unavailable"))).restart_preview(order=self.order)
        self.assertIn("provider unavailable", str(ctx.exception))
        self.assertEqual(ctx.exception.failure["failure_class"], "unknown")
        self.assertNotIn("worker", ctx.exception.failure)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)
        # a retry from PREVIEW_GENERATING is attempt 3
        with self.assertRaises(GenerationError) as ctx:
            self.service(FakeProvider(fail=RuntimeError("provider unavailable"))).generate_preview(order=self.order)
        self.assertIn("provider unavailable", str(ctx.exception))
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 3)

    @override_settings(GENERATION_WORKER_ENABLED=True)
    def test_sync_wrapper_with_worker_enabled_reports_queued(self):
        with use_executor(RecordingExecutor()):
            with self.assertRaises(GenerationError) as ctx:
                self.service().generate_preview(order=self.order)
        self.assertIn("поставлена в очередь", str(ctx.exception))

    def test_restart_inside_outer_transaction_runs_inline(self):
        service = self.service()
        service.generate_preview(order=self.order)
        self.order.refresh_from_db()
        job = service.request_preview_restart(order=self.order)
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(job.attempt, 2)
        self.assertEqual(len(service.provider.requests), 2)


class RecordingExecutorTests(QueueTestCase):
    def test_enqueue_exactly_once_and_job_pending_until_run(self):
        service = self.service()
        with use_executor(RecordingExecutor()) as executor:
            job = service.request_preview(order=self.order)
            self.assertEqual(job.status, GenerationJob.Status.PENDING)
            self.assertIsNone(job.started_at)
            self.assertEqual(executor.dispatched, [job.pk])
            self.assertEqual(service.provider.requests, [])
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)

            # second click while queued → refused, nothing enqueued again
            with self.assertRaises(GenerationError) as ctx:
                service.request_preview(order=self.order)
            self.assertEqual(str(ctx.exception), QUEUED_MESSAGE.format(job_id=job.pk))
            self.assertEqual(executor.dispatched, [job.pk])

            executor.run_all(service=service)
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(len(service.provider.requests), 1)

    def test_budget_rollback_means_no_job_and_no_dispatch(self):
        service = self.service()
        with use_executor(RecordingExecutor()) as executor:
            with mock.patch("apps.core.services.generation.BudgetGuard.enforce", side_effect=GenerationError("budget")):
                with self.assertRaises(GenerationError):
                    service.request_preview(order=self.order)
            self.assertEqual(executor.dispatched, [])
        self.assertFalse(GenerationJob.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)


class ClaimAndRunJobTests(QueueTestCase):
    def _pending(self, service):
        with use_executor(RecordingExecutor()):
            return service.request_preview(order=self.order)

    def test_worker_runs_job_exactly_once(self):
        service = self.service()
        job = self._pending(service)
        run_job(job.pk, service=service)
        run_job(job.pk, service=service)  # redelivered message → no-op
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(len(service.provider.requests), 1)

    def test_claim_is_the_gate(self):
        job = self._pending(self.service())
        first = claim(job.pk)
        self.assertEqual(first.status, GenerationJob.Status.RUNNING)
        self.assertEqual(first.output_metadata["worker"]["executor"], "recording")
        self.assertIsNone(claim(job.pk))
        self.assertIsNone(claim(job.pk + 1000))

    def test_fresh_running_refuses_new_attempt(self):
        service = self.service()
        job = self._pending(service)
        claim(job.pk)
        with self.assertRaises(GenerationError) as ctx:
            service.request_preview(order=self.order)
        self.assertEqual(str(ctx.exception), ALREADY_RUNNING_MESSAGE)
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.RUNNING)  # not reaped

    def test_worker_crash_stale_running_reaped_as_ambiguous(self):
        service = self.service()
        job = self._pending(service)
        claim(job.pk)
        GenerationJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(minutes=16))
        reaped = reap_stale(self.order)
        self.assertEqual([j.pk for j in reaped], [job.pk])
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], "ambiguous")
        self.assertTrue(job.output_metadata["worker"]["picked_at"])  # picked and died
        self.assertEqual(job_cost(job.input_metadata)["billing_outcome"], BillingOutcome.TIMEOUT_AMBIGUOUS)
        # the operator may start again
        next_job = service.request_preview(order=self.order)
        self.assertEqual(next_job.status, GenerationJob.Status.SUCCEEDED)

    def test_run_job_on_stale_running_reaps_and_skips(self):
        service = self.service()
        job = self._pending(service)
        claim(job.pk)
        GenerationJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(minutes=16))
        run_job(job.pk, service=service)
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(service.provider.requests, [])

    def test_pending_is_never_reaped_automatically(self):
        service = self.service()
        job = self._pending(service)
        GenerationJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(reap_stale(self.order), [])
        with self.assertRaises(GenerationError) as ctx:
            service.request_preview(order=self.order)
        self.assertIn("поставлена в очередь", str(ctx.exception))

    def test_provider_failure_recorded_with_worker_facts(self):
        service = self.service(FakeProvider(fail=RuntimeError("boom")))
        job = self._pending(service)
        run_job(job.pk, service=service)
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.error, "boom")
        self.assertEqual(job.output_metadata["failure_class"], "unknown")
        self.assertTrue(job.output_metadata["worker"]["finished_at"])

    def test_run_job_skips_full_tasks_in_c1(self):
        job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.FULL, status=GenerationJob.Status.PENDING,
            attempt=1, slot_key="hello", provider="fake",
        )
        service = self.service()
        run_job(job.pk, service=service)
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.PENDING)  # not claimed: nothing left RUNNING
        self.assertEqual(service.provider.requests, [])


class DequeueTests(QueueTestCase):
    def _pending(self, minutes_ago=6):
        service = self.service()
        with use_executor(RecordingExecutor()):
            job = service.request_preview(order=self.order)
        GenerationJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        job.refresh_from_db()
        return service, job

    def test_dequeue_stale_pending_is_queue_lost_not_billable_retryable(self):
        service, job = self._pending()
        dequeued = dequeue(job)
        self.assertEqual(dequeued.status, GenerationJob.Status.FAILED)
        self.assertEqual(dequeued.output_metadata["failure_class"], QUEUE_LOST)
        cost = job_cost(dequeued.input_metadata)
        self.assertEqual(cost["billing_outcome"], BillingOutcome.BEFORE_PROVIDER)
        self.assertIs(cost["billable"], False)
        # a worker arriving later skips it
        run_job(job.pk, service=service)
        self.assertEqual(service.provider.requests, [])
        # and the operator may start again
        self.assertEqual(service.request_preview(order=self.order).status, GenerationJob.Status.SUCCEEDED)

    def test_dequeue_refuses_fresh_running_and_finished(self):
        service, fresh = self._pending(minutes_ago=1)
        with self.assertRaises(DequeueError):
            dequeue(fresh)
        claim(fresh.pk)
        with self.assertRaises(DequeueError):
            dequeue(fresh)
        run_job(fresh.pk, service=service)
        with self.assertRaises(DequeueError):
            dequeue(fresh)

    def test_claim_vs_dequeue_race_one_wins(self):
        service, job = self._pending()
        # claim first → dequeue refused (already picked)
        self.assertIsNotNone(claim(job.pk))
        with self.assertRaises(DequeueError):
            dequeue(job)
        # dequeue first → claim returns None
        _, other = self._pending_second_order()
        dequeue(other)
        self.assertIsNone(claim(other.pk))

    def _pending_second_order(self):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="max", external_user_id="q-user-2")
        order = Order.objects.create(
            user=user, channel_identity=identity, product=self.order.product, style=self.order.style,
            status=Order.Status.PAID,
        )
        key = f"orders/{order.pk}/reference.jpg"
        self.storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(order=order, storage_key=key, original_filename="r.jpg", mime_type="image/jpeg", size_bytes=12)
        service = self.service()
        with use_executor(RecordingExecutor()):
            job = service.request_preview(order=order)
        GenerationJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=6))
        job.refresh_from_db()
        return service, job


class RQExecutorTests(QueueTestCase):
    def test_enqueue_after_commit_with_job_id_timeout_and_no_retry(self):
        fake_queue = mock.Mock()
        fake_queue.enqueue_call.return_value = mock.Mock(id="generation-job-x")
        service = self.service()
        with use_executor(RQExecutor(queue=fake_queue)):
            with self.captureOnCommitCallbacks(execute=True):
                job = service.request_preview(order=self.order)
        fake_queue.enqueue_call.assert_called_once()
        kwargs = fake_queue.enqueue_call.call_args.kwargs
        self.assertEqual(kwargs["func"], "apps.core.services.generation_queue.run_job")
        self.assertEqual(kwargs["args"], (job.pk,))
        self.assertEqual(kwargs["timeout"], 560)
        self.assertIsNone(kwargs["retry"])
        self.assertEqual(kwargs["job_id"], f"generation-job-{job.pk}")
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.PENDING)
        self.assertEqual(job.input_metadata["queue"]["executor"], "rq")
        self.assertEqual(job.input_metadata["queue"]["rq_job_id"], "generation-job-x")
        self.assertEqual(service.provider.requests, [])  # no provider call in web

    def test_enqueue_deferred_until_commit(self):
        fake_queue = mock.Mock()
        fake_queue.enqueue_call.return_value = mock.Mock(id="x")
        service = self.service()
        with use_executor(RQExecutor(queue=fake_queue)):
            with self.captureOnCommitCallbacks() as callbacks:
                with transaction.atomic():
                    service.request_preview(order=self.order)
                    fake_queue.enqueue_call.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_enqueue_failure_leaves_pending_and_reports(self):
        fake_queue = mock.Mock()
        fake_queue.enqueue_call.side_effect = ConnectionError("redis down")
        service = self.service()
        with use_executor(RQExecutor(queue=fake_queue)):
            # autocommit in production runs the callback right away; the test
            # transaction defers it to the capture block's exit
            with self.assertRaises(generation_queue.QueueError):
                with self.captureOnCommitCallbacks(execute=True):
                    service.request_preview(order=self.order)
        job = GenerationJob.objects.get(order=self.order)
        self.assertEqual(job.status, GenerationJob.Status.PENDING)

    @override_settings(GENERATION_WORKER_ENABLED=True)
    def test_flag_selects_rq_executor(self):
        self.assertIsInstance(generation_queue.get_executor(), RQExecutor)
