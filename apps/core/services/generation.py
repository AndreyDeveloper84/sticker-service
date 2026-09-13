from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.image_providers import ImageGenerationRequest, ImageProvider, ReferenceImage
from apps.core.models import GeneratedAsset, GenerationJob, Order, OrderPhoto, Revision
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.storage import LocalMediaStorage


class GenerationError(ValueError):
    pass


class GenerationService:
    def __init__(self, *, provider: ImageProvider, storage=None):
        self.provider = provider
        self.storage = storage or LocalMediaStorage()

    def generate_preview(self, *, order: Order) -> GeneratedAsset:
        job = self._start_job(order=order, task_type=GenerationJob.TaskType.PREVIEW)
        return self._run_job(job)

    def generate_revision(self, *, order: Order) -> GeneratedAsset:
        job = self._start_job(order=order, task_type=GenerationJob.TaskType.REVISION)
        return self._run_job(job)

    def _run_job(self, job):
        try:
            request = self._build_request(job=job)
            result = self.provider.generate_preview(request)
            if not result.content:
                raise GenerationError("Image provider returned empty content")
            return self._complete(job=job, result=result)
        except Exception as exc:
            self._fail_job(job=job, exc=exc)
            if isinstance(exc, GenerationError):
                raise
            raise GenerationError(str(exc)) from exc

    @transaction.atomic
    def _start_job(self, *, order: Order, task_type: str) -> GenerationJob:
        locked_order = (
            Order.objects.select_for_update()
            .select_related("product", "style")
            .get(pk=order.pk)
        )
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

        return GenerationJob.objects.create(
            order=locked_order,
            task_type=task_type,
            status=GenerationJob.Status.RUNNING,
            attempt=latest_attempt + 1,
            provider=self.provider.name,
            input_metadata=input_metadata,
            started_at=timezone.now(),
        )

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
        ]
        if order.customer_notes.strip():
            prompt_parts.append(f"Customer notes: {order.customer_notes.strip()}")
        if order.operator_notes.strip():
            prompt_parts.append(f"Operator notes: {order.operator_notes.strip()}")
        if job.task_type == GenerationJob.TaskType.REVISION:
            revision = Revision.objects.get(order=order)
            prompt_parts.append(f"Revision category: {revision.category}")
            if revision.customer_text.strip():
                prompt_parts.append(f"Customer revision request: {revision.customer_text.strip()}")

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

        return ImageGenerationRequest(
            prompt="\n".join(part for part in prompt_parts if part),
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
        asset = GeneratedAsset.objects.create(
            order=locked_order,
            job=locked_job,
            kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=storage_key,
            mime_type=result.mime_type or "image/png",
            size_bytes=len(result.content),
            metadata=metadata,
        )
        locked_job.status = GenerationJob.Status.SUCCEEDED
        locked_job.output_metadata = {"asset_id": asset.pk, **metadata}
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "output_metadata", "finished_at", "updated_at"]
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
    def _fail_job(self, *, job: GenerationJob, exc: Exception) -> None:
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            return
        locked_job.status = GenerationJob.Status.FAILED
        locked_job.error = str(exc)[:4000]
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "error", "finished_at", "updated_at"]
        )
