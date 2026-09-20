from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from uuid import uuid4

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.image_providers import (
    ImageGenerationRequest,
    ImageProvider,
    ReferenceImage,
    describe_provider_failure,
)
from apps.core.models import GeneratedAsset, GenerationJob, Order, OrderPhoto, Revision
from apps.core.services.generation_prompts import FRAMING_CLAUSE, SAFE_FOR_WORK_CLAUSE, render_revision_request
from apps.core.services import generation_queue
from apps.core.services.budget import BudgetGuard, BudgetOverride
from apps.core.services import generation_cost
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.storage import LocalMediaStorage


class GenerationError(ValueError):
    """Domain error; ``failure`` carries the provider failure facts
    (failure_class, moderation_*) when the provider call itself failed."""

    failure: dict | None = None


ALREADY_RUNNING_MESSAGE = "Генерация уже выполняется, дождитесь завершения (~1–1.5 мин)"
QUEUED_MESSAGE = "Генерация уже поставлена в очередь (job #{job_id}), дождитесь worker'а"

# A RUNNING job older than this is a dead worker (gunicorn kill, OOM, host
# restart): it is failed closed as "ambiguous" so the operator is not locked
# out forever. A PENDING job is never reaped automatically (decision C-2):
# the operator dequeues it explicitly (generation_queue.dequeue).
STALE_RUNNING_AFTER = timedelta(minutes=15)


def reap_stale(order: Order) -> list[GenerationJob]:
    """Fail closed every RUNNING job of the order older than
    STALE_RUNNING_AFTER (any task type). Returns the jobs marked. Used by the
    RUNNING guard, the worker before claim and the console card."""
    stale_before = timezone.now() - STALE_RUNNING_AFTER
    reaped = []
    with transaction.atomic():
        running = GenerationJob.objects.select_for_update().filter(
            order=order, status=GenerationJob.Status.RUNNING
        )
        for job in running:
            started = job.started_at or job.created_at
            if not (started and started < stale_before):
                continue
            job.status = GenerationJob.Status.FAILED
            job.error = "stale RUNNING job (worker gone); failed closed"
            job.output_metadata = {**(job.output_metadata or {}), "failure_class": "ambiguous"}
            generation_cost.apply_failure(job, job.output_metadata)
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error", "output_metadata", "input_metadata", "finished_at", "updated_at"])
            reaped.append(job)
    return reaped


class GenerationService:
    def __init__(self, *, provider: ImageProvider, storage=None):
        self.provider = provider
        self.storage = storage or LocalMediaStorage()

    # ------------------------------------------------ async API (C-1)
    # request_*: create the PENDING attempt (guards + budget + snapshot in one
    # transaction) and hand it to the executor. Returns the job; with the
    # inline executor the job is already finished on return.

    def request_preview(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GenerationJob:
        job = self._start_job(order=order, task_type=GenerationJob.TaskType.PREVIEW, budget_override=budget_override)
        return self._dispatch(job)

    def request_revision(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GenerationJob:
        job = self._start_job(order=order, task_type=GenerationJob.TaskType.REVISION, budget_override=budget_override)
        return self._dispatch(job)

    def request_preview_restart(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GenerationJob:
        """Console «Перегенерировать превью»: INTERNAL_PREVIEW_REVIEW →
        PREVIEW_GENERATING and the new attempt in ONE transaction, so a
        budget refusal (or any other error before the job exists) leaves
        the order on internal review instead of a dangling PREVIEW_GENERATING."""
        with transaction.atomic():
            OrderStateService.transition(order=order, to_status=Order.Status.PREVIEW_GENERATING)
            job = self._start_job(
                order=order, task_type=GenerationJob.TaskType.PREVIEW, budget_override=budget_override
            )
        return self._dispatch(job)

    def _dispatch(self, job: GenerationJob) -> GenerationJob:
        generation_queue.dispatch(job, service=self)
        job.refresh_from_db()
        return job

    def execute_claimed(self, job: GenerationJob) -> GeneratedAsset | None:
        """Worker side: ``job`` is RUNNING (claimed). Builds the request, calls
        the provider once and records the outcome on the job; never raises
        for provider failures (the job carries them)."""
        try:
            request = self._build_request(job=job)
            result = self.provider.generate_preview(request)
            if not result.content:
                raise GenerationError("Image provider returned empty content")
            return self._complete(job=job, result=result)
        except Exception as exc:
            self._fail_job(job=job, exc=exc)
            return None

    # ------------------------------------------------ sync API
    # generate_* / restart_preview = request + result: the historical contract
    # (asset or GenerationError). Meaningful with the inline executor only;
    # with the background worker enabled the console uses request_*.

    def generate_preview(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GeneratedAsset:
        return self._sync_result(self.request_preview(order=order, budget_override=budget_override))

    def generate_revision(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GeneratedAsset:
        return self._sync_result(self.request_revision(order=order, budget_override=budget_override))

    def restart_preview(self, *, order: Order, budget_override: BudgetOverride | None = None) -> GeneratedAsset:
        return self._sync_result(self.request_preview_restart(order=order, budget_override=budget_override))

    @staticmethod
    def _sync_result(job: GenerationJob) -> GeneratedAsset:
        if job.status == GenerationJob.Status.SUCCEEDED:
            return GeneratedAsset.objects.get(pk=(job.output_metadata or {})["asset_id"])
        if job.status == GenerationJob.Status.FAILED:
            error = GenerationError(job.error or f"Job #{job.pk} failed")
            error.failure = {
                key: value
                for key, value in (job.output_metadata or {}).items()
                if key != generation_queue.WORKER_KEY
            }
            raise error
        raise GenerationError(QUEUED_MESSAGE.format(job_id=job.pk))

    @transaction.atomic
    def _start_job(
        self, *, order: Order, task_type: str, budget_override: BudgetOverride | None = None
    ) -> GenerationJob:
        locked_order = (
            Order.objects.select_for_update()
            .select_related("product", "style")
            .get(pk=order.pk)
        )
        # DRF-2089: one billable attempt at a time. A second click while the
        # first request is still running (browser 499, double submit) must
        # not open a parallel provider call.
        self._guard_running(locked_order=locked_order, task_type=task_type)
        if task_type == GenerationJob.TaskType.PREVIEW:
            if locked_order.status == Order.Status.PAID:
                OrderStateService.transition(
                    order=locked_order,
                    to_status=Order.Status.PREVIEW_GENERATING,
                )
            elif locked_order.status != Order.Status.PREVIEW_GENERATING:
                raise GenerationError(
                    f"Order #{locked_order.pk} cannot generate preview from {locked_order.status}"
                )
        elif task_type == GenerationJob.TaskType.REVISION:
            if locked_order.status == Order.Status.REVISION_REQUESTED:
                OrderStateService.transition(
                    order=locked_order,
                    to_status=Order.Status.REVISION_GENERATING,
                )
            elif locked_order.status != Order.Status.REVISION_GENERATING:
                raise GenerationError(
                    f"Order #{locked_order.pk} cannot generate revision from {locked_order.status}"
                )
            if not Revision.objects.filter(order=locked_order).exists():
                raise GenerationError("Order has no revision request")
        else:
            raise GenerationError("Unsupported generation task")

        latest_attempt = (
            GenerationJob.objects.filter(
                order=locked_order,
                task_type=task_type,
            ).aggregate(max_attempt=Max("attempt"))["max_attempt"]
            or 0
        )
        photo_ids = list(
            locked_order.photos.exclude(status=OrderPhoto.Status.REJECTED)
            .order_by("created_at", "pk")
            .values_list("id", flat=True)
        )
        if not photo_ids:
            raise GenerationError("Order has no usable reference photos")

        input_metadata = {
            "product": locked_order.product.code,
            "style": locked_order.style.code,
            "photo_ids": photo_ids,
        }
        if task_type == GenerationJob.TaskType.REVISION:
            revision = Revision.objects.get(order=locked_order)
            input_metadata.update(
                {
                    "revision_id": revision.pk,
                    "source_preview_id": revision.source_preview_id,
                    "revision_category": revision.category,
                }
            )
            revision.status = Revision.Status.GENERATING
            revision.save(update_fields=["status", "updated_at"])

        # Pilot Budget Guard (DRF-2086): last statement before the billable
        # attempt exists. Raises BudgetExceeded / BudgetConfigError → this
        # transaction rolls back → no RUNNING job, no provider call.
        BudgetGuard(override=budget_override).enforce(locked_order, task_type)
        # DRF-2111: immutable price snapshot taken now, before the provider
        # is called; the outcome is appended by _complete/_fail_job.
        input_metadata[generation_cost.COST_KEY] = generation_cost.cost_snapshot(
            provider=self.provider, task_type=task_type
        )

        # PENDING = created, not yet picked up by the executor; started_at is
        # set by generation_queue.claim (PENDING → RUNNING) in the worker.
        input_metadata["queue"] = {"requested_at": timezone.now().isoformat()}
        return GenerationJob.objects.create(
            order=locked_order,
            task_type=task_type,
            status=GenerationJob.Status.PENDING,
            attempt=latest_attempt + 1,
            provider=self.provider.name,
            input_metadata=input_metadata,
        )

    @staticmethod
    def _guard_running(*, locked_order: Order, task_type: str) -> None:
        """One billable attempt at a time: a fresh RUNNING job or a PENDING
        (queued) job refuses a new attempt; only a stale RUNNING job is
        reaped (ambiguous). PENDING is never reaped here — see dequeue."""
        reap_stale(locked_order)
        active = (
            GenerationJob.objects.select_for_update()
            .filter(
                order=locked_order,
                task_type=task_type,
                status__in=[GenerationJob.Status.PENDING, GenerationJob.Status.RUNNING],
            )
            .order_by("pk")
            .first()
        )
        if active is None:
            return
        if active.status == GenerationJob.Status.PENDING:
            raise GenerationError(QUEUED_MESSAGE.format(job_id=active.pk))
        raise GenerationError(ALREADY_RUNNING_MESSAGE)

    def _build_request(self, *, job: GenerationJob) -> ImageGenerationRequest:
        order = (
            Order.objects.select_related("product", "style")
            .prefetch_related("photos")
            .get(pk=job.order_id)
        )
        product_config = order.product.config or {}
        style_config = order.style.config or {}
        prompt_parts = [
            str(product_config.get("generation_prompt") or "Create a personalized preview image based on the reference photos."),
            str(style_config.get("prompt") or f"Use the {order.style.name} style."),
            SAFE_FOR_WORK_CLAUSE,
            FRAMING_CLAUSE,
        ]
        if order.customer_notes.strip():
            prompt_parts.append(f"Customer notes: {order.customer_notes.strip()}")
        if order.operator_notes.strip():
            prompt_parts.append(f"Operator notes: {order.operator_notes.strip()}")
        if job.task_type == GenerationJob.TaskType.REVISION:
            revision = Revision.objects.get(order=order)
            prompt_parts.append(render_revision_request(revision.category, revision.customer_text))

        references = []
        photos = order.photos.exclude(status=OrderPhoto.Status.REJECTED).order_by("created_at", "pk")
        for photo in photos:
            with self.storage.open(photo.storage_key, "rb") as source:
                references.append(
                    ReferenceImage(
                        filename=photo.original_filename or f"photo-{photo.pk}.jpg",
                        mime_type=photo.mime_type or "image/jpeg",
                        content=source.read(),
                    )
                )
        if not references:
            raise GenerationError("Order has no usable reference photos")

        # DRF-2080: persist the rendered prompt as live evidence.
        prompt = "\n".join(part for part in prompt_parts if part)
        job.input_metadata = {**(job.input_metadata or {}), "prompt": prompt}
        job.save(update_fields=["input_metadata", "updated_at"])

        return ImageGenerationRequest(
            prompt=prompt,
            reference_images=references,
            metadata={
                "order_id": order.pk,
                "job_id": job.pk,
                "task_type": job.task_type,
                "product": order.product.code,
                "style": order.style.code,
            },
        )

    @transaction.atomic
    def _complete(self, *, job: GenerationJob, result) -> GeneratedAsset:
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            raise GenerationError(f"Job #{locked_job.pk} is not running")
        locked_order = Order.objects.select_for_update().get(pk=locked_job.order_id)
        expected_status = (
            Order.Status.PREVIEW_GENERATING
            if locked_job.task_type == GenerationJob.TaskType.PREVIEW
            else Order.Status.REVISION_GENERATING
        )
        if locked_order.status != expected_status:
            raise GenerationError(
                f"Order #{locked_order.pk} is not generating {locked_job.task_type}: {locked_order.status}"
            )

        storage_key = (
            f"generated/order-{locked_order.pk}/preview/"
            f"job-{locked_job.pk}-{uuid4().hex}.png"
        )
        self.storage.save(storage_key, BytesIO(result.content))
        metadata = {**(result.metadata or {}), "task_type": locked_job.task_type}
        # the outbound route is job evidence (worker.proxy), not asset metadata
        asset_metadata = {key: value for key, value in metadata.items() if key != "proxy"}
        asset = GeneratedAsset.objects.create(
            order=locked_order,
            job=locked_job,
            kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=storage_key,
            mime_type=result.mime_type or "image/png",
            size_bytes=len(result.content),
            metadata=asset_metadata,
        )
        locked_job.status = GenerationJob.Status.SUCCEEDED
        # keep the worker facts written at claim (picked_at, host, pid)
        locked_job.output_metadata = {**(locked_job.output_metadata or {}), "asset_id": asset.pk, **metadata}
        generation_cost.apply_success(locked_job, metadata)
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "output_metadata", "input_metadata", "finished_at", "updated_at"]
        )
        if locked_job.task_type == GenerationJob.TaskType.REVISION:
            revision = Revision.objects.select_for_update().get(order=locked_order)
            revision.status = Revision.Status.COMPLETED
            revision.save(update_fields=["status", "updated_at"])
        OrderStateService.transition(
            order=locked_order,
            to_status=Order.Status.INTERNAL_PREVIEW_REVIEW,
        )
        return asset

    @transaction.atomic
    def _fail_job(self, *, job: GenerationJob, exc: Exception) -> dict | None:
        """Mark the job FAILED; returns the provider failure facts recorded
        in output_metadata (failure_class, moderation_*), None if the job
        was not RUNNING any more."""
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            return None
        failure = describe_provider_failure(self.provider, exc)
        locked_job.status = GenerationJob.Status.FAILED
        locked_job.error = str(exc)[:4000]
        locked_job.output_metadata = {**(locked_job.output_metadata or {}), **failure}
        generation_cost.apply_failure(locked_job, failure)
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "error", "output_metadata", "input_metadata", "finished_at", "updated_at"]
        )
        return failure
