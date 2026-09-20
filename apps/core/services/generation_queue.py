"""Background generation: executor + worker entry (async design 2026-09-20, C-1).

Split point. The console request does everything that must stay inside the
job-creation transaction — order lock, RUNNING guard, status transition,
BudgetGuard.enforce, cost snapshot, GenerationJob(PENDING) — and then
``dispatch(job)``. Everything after that (prompt, reference photos, the
provider call, _complete / _fail_job) runs in ``run_job(job_id)``, either
inline (same process, default) or in the ``generation_worker`` RQ process.

At-most-once per attempt is a DATABASE property, not a queue property:
``claim`` moves PENDING → RUNNING under select_for_update; a redelivered or
duplicated message finds a non-PENDING job and returns without any provider
call. Redis carries job ids only — never prompts, photos or results.

Executors:
- InlineExecutor  (GENERATION_WORKER_ENABLED=false): run_job right away in the
  calling process. Today's behaviour; the mode of tests, CI and local dev.
- RQExecutor      (GENERATION_WORKER_ENABLED=true): enqueue the job id on the
  "generation" RQ queue after the creating transaction commits.
- RecordingExecutor: test double — records dispatches, runs them on demand.
"""

from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.models import GenerationJob
from apps.core.services import generation_cost

logger = logging.getLogger(__name__)

# A PENDING job the worker has not picked up for this long is shown by the
# console as "waiting for the worker" and may be dequeued by the operator
# (explicit action, never automatic — decision C-2).
QUEUE_WAIT_WARN_AFTER = timedelta(minutes=5)

# failure_class of a dequeued job: the provider was never called.
QUEUE_LOST = "queue_lost"

WORKER_KEY = "worker"


class QueueError(RuntimeError):
    """The job could not be handed to the background queue (Redis down, RQ
    refused). The PENDING job stays in the database for the operator."""


# ------------------------------------------------------------------ executors


class InlineExecutor:
    name = "inline"

    def dispatch(self, job: GenerationJob, *, service=None) -> None:
        # Same process, same connection: the row is visible even inside an
        # outer transaction, so nothing is deferred (D-3 applies to RQ). The
        # creating service (its provider / storage) performs the call.
        run_job(job.pk, service=service)


class RQExecutor:
    name = "rq"

    def __init__(self, *, queue=None):
        self._queue = queue

    def queue(self):
        if self._queue is None:
            self._queue = rq_queue()
        return self._queue

    def dispatch(self, job: GenerationJob, *, service=None) -> None:
        # The worker is another process: enqueue only once the creating
        # transaction is committed, or it would find no row / a stale row.
        # ``service`` is ignored on purpose: the worker builds its own from
        # job.provider (no objects cross the process boundary).
        transaction.on_commit(lambda: self._enqueue(job))

    def _enqueue(self, job: GenerationJob) -> None:
        try:
            rq_job = self.queue().enqueue_call(
                func="apps.core.services.generation_queue.run_job",
                args=(job.pk,),
                timeout=settings.GENERATION_JOB_TIMEOUT_S,
                result_ttl=0,
                failure_ttl=7 * 24 * 3600,
                retry=None,  # never an automatic retry: at-most-once per attempt
                job_id=rq_job_id(job),
                description=f"generation job {job.pk} {job.task_type} {job.slot_key}".strip(),
            )
        except Exception as exc:  # redis / rq failure
            logger.error("generation.queue.enqueue_failed job=%s error=%s", job.pk, exc)
            raise QueueError(f"Очередь генерации недоступна, job #{job.pk} остался в ожидании: {exc}") from exc
        _stamp_queue(job, {"executor": self.name, "enqueued_at": timezone.now().isoformat(), "rq_job_id": rq_job.id})


class RecordingExecutor:
    """Test double: records dispatched job ids; ``run_all()`` executes them."""

    name = "recording"

    def __init__(self):
        self.dispatched: list[int] = []

    def dispatch(self, job: GenerationJob, *, service=None) -> None:
        self.dispatched.append(job.pk)

    def run_all(self, *, service=None) -> None:
        pending = list(self.dispatched)
        self.dispatched.clear()
        for job_id in pending:
            run_job(job_id, service=service)


def rq_job_id(job: GenerationJob) -> str:
    """Stable RQ id per attempt (traceable in `rq info`). RQ does not
    deduplicate on it — a second message is harmless because ``claim`` is
    the gate, not the queue."""
    return f"generation-job-{job.pk}"


def rq_connection():
    import redis

    return redis.Redis.from_url(settings.GENERATION_QUEUE_REDIS_URL)


def rq_queue(connection=None):
    from rq import Queue

    return Queue(settings.GENERATION_QUEUE_NAME, connection=connection or rq_connection())


def worker_enabled() -> bool:
    return bool(getattr(settings, "GENERATION_WORKER_ENABLED", False))


_executor_override = None


def get_executor():
    if _executor_override is not None:
        return _executor_override
    return RQExecutor() if worker_enabled() else InlineExecutor()


@contextmanager
def use_executor(executor):
    """Tests: force an executor regardless of settings."""
    global _executor_override
    previous = _executor_override
    _executor_override = executor
    try:
        yield executor
    finally:
        _executor_override = previous


def dispatch(job: GenerationJob, *, service=None) -> None:
    """Hand a PENDING job to the configured executor. ``service`` is the
    creating GenerationService (used by the inline path only)."""
    executor = get_executor()
    if executor.name != RQExecutor.name:
        _stamp_queue(job, {"executor": executor.name, "enqueued_at": timezone.now().isoformat()})
    executor.dispatch(job, service=service)


def _stamp_queue(job: GenerationJob, facts: dict) -> None:
    GenerationJob.objects.filter(pk=job.pk).update(
        input_metadata={**(job.input_metadata or {}), "queue": {**((job.input_metadata or {}).get("queue") or {}), **facts}},
        updated_at=timezone.now(),
    )
    job.refresh_from_db(fields=["input_metadata"])


# ------------------------------------------------------------ worker entry


@transaction.atomic
def claim(job_id: int) -> GenerationJob | None:
    """PENDING → RUNNING under select_for_update. None when the job is not
    PENDING any more (already claimed, finished, dequeued): the caller must
    NOT call the provider — this is the at-most-once gate."""
    job = GenerationJob.objects.select_for_update().filter(pk=job_id).first()
    if job is None or job.status != GenerationJob.Status.PENDING:
        return None
    job.status = GenerationJob.Status.RUNNING
    job.started_at = timezone.now()
    job.output_metadata = {
        **(job.output_metadata or {}),
        WORKER_KEY: {
            "picked_at": job.started_at.isoformat(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "executor": (job.input_metadata or {}).get("queue", {}).get("executor", ""),
        },
    }
    job.save(update_fields=["status", "started_at", "output_metadata", "updated_at"])
    return job


def run_job(job_id: int, *, service=None) -> None:
    """Worker entry (RQ target and inline path). Never raises for a job that is
    not PENDING; provider failures are recorded on the job by the services.
    ``service``: the GenerationService to use (inline / tests); the RQ worker
    passes none and builds one from ``job.provider``."""
    from apps.core.services.generation import GenerationService, reap_stale

    job = GenerationJob.objects.select_related("order").filter(pk=job_id).first()
    if job is None:
        logger.warning("generation.worker.skip job=%s reason=missing", job_id)
        return
    if job.task_type not in (GenerationJob.TaskType.PREVIEW, GenerationJob.TaskType.REVISION, GenerationJob.TaskType.FULL):
        logger.error("generation.worker.skip job=%s reason=unsupported_task task=%s", job_id, job.task_type)
        return
    reap_stale(job.order)
    job = claim(job_id)
    if job is None:
        logger.info("generation.worker.skip job=%s reason=not_pending", job_id)
        return
    if service is None:
        from apps.core.image_providers import get_image_provider

        provider = get_image_provider(job.provider)
        if job.task_type == GenerationJob.TaskType.FULL:
            from apps.core.services.full_production import FullProductionService

            service = FullProductionService(provider=provider)
        else:
            service = GenerationService(provider=provider)
    try:
        # GenerationService (preview / revision) or FullProductionService
        # (FULL slot, continues the lazy chain after a success)
        service.execute_claimed(job)
    finally:
        _stamp_finished(job)


def _stamp_finished(job: GenerationJob) -> None:
    """worker.finished_at / duration_s / proxy on the job, after the services
    recorded the outcome (separate, tiny transaction)."""
    fresh = GenerationJob.objects.filter(pk=job.pk).first()
    if fresh is None:
        return
    output = dict(fresh.output_metadata or {})
    worker = dict(output.get(WORKER_KEY) or {})
    finished = fresh.finished_at or timezone.now()
    worker["finished_at"] = finished.isoformat()
    if fresh.started_at:
        worker["duration_s"] = round((finished - fresh.started_at).total_seconds(), 1)
    proxy = output.pop("proxy", None)
    if proxy:
        worker["proxy"] = proxy
    output[WORKER_KEY] = worker
    GenerationJob.objects.filter(pk=fresh.pk).update(output_metadata=output, updated_at=timezone.now())
    logger.info(
        "generation.job.finished job=%s order=%s task=%s slot=%s status=%s class=%s duration_s=%s proxy=%s",
        fresh.pk, fresh.order_id, fresh.task_type, fresh.slot_key or "-", fresh.status,
        output.get("failure_class", "-"), worker.get("duration_s", "-"), worker.get("proxy", "-"),
    )


# ------------------------------------------------------------ operator action


class DequeueError(ValueError):
    pass


def queue_wait(job: GenerationJob, now=None) -> timedelta | None:
    """How long a PENDING job has waited for a worker; None for other states."""
    if job.status != GenerationJob.Status.PENDING:
        return None
    since = job.created_at
    return (now or timezone.now()) - since


@transaction.atomic
def dequeue(job: GenerationJob) -> GenerationJob:
    """Console «Снять из очереди»: a PENDING job nobody picked up for
    QUEUE_WAIT_WARN_AFTER is failed as ``queue_lost`` — the provider was never
    called (before_provider, not billable), so the slot is retryable. Anything
    else (RUNNING, already picked, too fresh, finished) is refused. A worker
    arriving later finds a non-PENDING job and skips it (see ``claim``)."""
    locked = GenerationJob.objects.select_for_update().get(pk=job.pk)
    if locked.status != GenerationJob.Status.PENDING:
        raise DequeueError(f"Job #{locked.pk} не в очереди (статус {locked.status}); снять можно только ожидающую задачу")
    if (locked.output_metadata or {}).get(WORKER_KEY, {}).get("picked_at"):
        raise DequeueError(f"Job #{locked.pk} уже взят worker'ом")
    waited = queue_wait(locked)
    if waited is None or waited < QUEUE_WAIT_WARN_AFTER:
        raise DequeueError(
            f"Job #{locked.pk} ждёт worker'а меньше {int(QUEUE_WAIT_WARN_AFTER.total_seconds() // 60)} мин — подождите"
        )
    failure = {"failure_class": QUEUE_LOST, "reason": "dequeued by operator"}
    locked.status = GenerationJob.Status.FAILED
    locked.error = "dequeued by operator: no worker picked the job up"
    locked.output_metadata = {**(locked.output_metadata or {}), **failure}
    generation_cost.apply_failure(locked, failure)
    locked.finished_at = timezone.now()
    locked.save(update_fields=["status", "error", "output_metadata", "input_metadata", "finished_at", "updated_at"])
    return locked
