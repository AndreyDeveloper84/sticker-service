from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from uuid import uuid4

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.image_providers import (
    ImageGenerationRequest,
    ImageProvider,
    ReferenceImage,
    classify_provider_failure,
)
from apps.core.models import GeneratedAsset, GenerationJob, Order, OrderPhoto, Payment
from apps.core.services.channel_order_flow import (
    order_emotion_codes,
    product_emotion_options,
)
from apps.core.services.order_state import OrderStateService
from apps.core.storage import LocalMediaStorage


class FullProductionError(ValueError):
    pass


@dataclass(frozen=True)
class SlotState:
    """Stable production-plan slot derived from FULL GenerationJob rows."""

    slot_key: str
    emotion: str
    status: str  # "pending" | "running" | "succeeded" | "failed"
    asset_id: int | None
    attempts: int
    retryable: bool


class FullProductionService:
    """Produces exactly the purchased volume after preview approval.

    One slot per selected emotion code; slot_key IS the emotion code, so
    re-entry maps onto the same slots and never duplicates successful
    assets. Identity consistency comes from the customer-approved preview,
    which is the first reference image of every slot job.
    """

    def __init__(self, *, provider: ImageProvider, storage=None):
        self.provider = provider
        self.storage = storage or LocalMediaStorage()

    # ------------------------------------------------------------- plan

    def expected_slots(self, order: Order) -> list[str]:
        config = order.product.config or {}
        try:
            quantity = int(config.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            raise FullProductionError(
                f"Product {order.product.code} has no production quantity"
            )
        codes = order_emotion_codes(order)
        if len(codes) != quantity or len(set(codes)) != len(codes):
            raise FullProductionError(
                f"Order #{order.pk} emotion selection does not match "
                f"product quantity {quantity}"
            )
        known = {option["code"] for option in product_emotion_options(order.product)}
        unknown = [code for code in codes if code not in known]
        if unknown:
            raise FullProductionError(
                f"Order #{order.pk} has emotions unknown to product "
                f"{order.product.code}: {', '.join(unknown)}"
            )
        return codes

    def production_plan(self, order: Order) -> list[SlotState]:
        slots = self.expected_slots(order)
        jobs_by_slot: dict[str, list[GenerationJob]] = {slot: [] for slot in slots}
        full_jobs = GenerationJob.objects.filter(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
        ).order_by("slot_key", "attempt")
        for job in full_jobs:
            jobs_by_slot.setdefault(job.slot_key, []).append(job)
        assets = {
            asset.slot_key: asset
            for asset in order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)
        }

        plan = []
        for slot in slots:
            slot_jobs = jobs_by_slot.get(slot, [])
            latest = slot_jobs[-1] if slot_jobs else None
            attempts = len(slot_jobs)
            asset = assets.get(slot)
            if asset is not None:
                plan.append(SlotState(slot, slot, "succeeded", asset.pk, attempts, False))
            elif latest is None:
                plan.append(SlotState(slot, slot, "pending", None, 0, True))
            elif latest.status == GenerationJob.Status.RUNNING:
                plan.append(SlotState(slot, slot, "running", None, attempts, False))
            elif latest.status == GenerationJob.Status.FAILED:
                failure_class = (latest.output_metadata or {}).get("failure_class")
                plan.append(
                    SlotState(slot, slot, "failed", None, attempts, failure_class != "ambiguous")
                )
            else:
                # SUCCEEDED without an asset is an inconsistent slot; expose
                # it as pending so a re-entry can repair it.
                plan.append(SlotState(slot, slot, "pending", None, attempts, True))
        return plan

    # ---------------------------------------------------------- entries

    def start(self, *, order: Order) -> list[SlotState]:
        """Run full production; safe to re-enter (no duplicate assets)."""
        jobs, _blocked = self._prepare(order=order, allow_entry=True, failed_only=False)
        self._run_jobs(jobs=jobs)
        return self._finalize(order=order)

    def retry_failed(self, *, order: Order) -> list[SlotState]:
        """Selective retry: failed retryable slots only, never succeeded ones.

        Slots whose latest failure was classified "ambiguous" (post-submit
        provider timeout) are skipped — a blind retry could duplicate
        billable generation — and reported via FullProductionError after
        the other slots have been processed.
        """
        jobs, blocked = self._prepare(order=order, allow_entry=False, failed_only=True)
        self._run_jobs(jobs=jobs)
        plan = self._finalize(order=order)
        if blocked:
            raise FullProductionError(
                "Slots need manual intervention (ambiguous provider failure, "
                f"no blind retry): {', '.join(blocked)}"
            )
        return plan

    # -------------------------------------------------------- internals

    @staticmethod
    def _approved_preview(order: Order):
        assets = order.generated_assets.filter(
            kind=GeneratedAsset.Kind.PREVIEW
        ).order_by("-created_at", "-pk")
        for asset in assets:
            if (asset.metadata or {}).get("customer_approved"):
                return asset
        return None

    def _prepare(self, *, order: Order, allow_entry: bool, failed_only: bool):
        """Create RUNNING attempts for slots that need (re)generation.

        Returns (new jobs, blocked slot keys). Blocked slots failed with an
        "ambiguous" classification and are never auto-regenerated.
        """
        with transaction.atomic():
            locked = (
                Order.objects.select_for_update()
                .select_related("product", "style")
                .get(pk=order.pk)
            )
            if locked.status == Order.Status.PREVIEW_REVIEW:
                if not allow_entry:
                    raise FullProductionError(
                        f"Order #{locked.pk} has not started full production yet; use start()"
                    )
                if not locked.payments.filter(status=Payment.Status.CONFIRMED).exists():
                    raise FullProductionError(
                        f"Order #{locked.pk} has no confirmed payment"
                    )
            elif locked.status != Order.Status.PACK_GENERATING:
                raise FullProductionError(
                    f"Order #{locked.pk} cannot run full production from {locked.status}"
                )

            preview = self._approved_preview(locked)
            if preview is None:
                raise FullProductionError(
                    f"Order #{locked.pk} has no customer-approved preview"
                )
            if locked.status == Order.Status.PREVIEW_REVIEW:
                OrderStateService.transition(
                    order=locked,
                    to_status=Order.Status.PACK_GENERATING,
                )

            slots = self.expected_slots(locked)
            photo_ids = list(
                locked.photos.exclude(status=OrderPhoto.Status.REJECTED)
                .order_by("created_at", "pk")
                .values_list("id", flat=True)
            )
            produced = set(
                locked.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)
                .values_list("slot_key", flat=True)
            )

            new_jobs = []
            blocked = []
            for slot in slots:
                if slot in produced:
                    continue  # preserve successful assets, never regenerate
                slot_jobs = GenerationJob.objects.filter(
                    order=locked,
                    task_type=GenerationJob.TaskType.FULL,
                    slot_key=slot,
                )
                latest = slot_jobs.order_by("-attempt").first()
                if latest is not None and latest.status == GenerationJob.Status.RUNNING:
                    # Crashed worker case: a stale RUNNING job holds no
                    # provider result, supersede it before the next attempt.
                    latest.status = GenerationJob.Status.FAILED
                    latest.error = "superseded by re-entry"
                    latest.finished_at = timezone.now()
                    latest.save(
                        update_fields=["status", "error", "finished_at", "updated_at"]
                    )
                elif latest is not None and latest.status == GenerationJob.Status.FAILED:
                    failure_class = (latest.output_metadata or {}).get("failure_class")
                    if failure_class == "ambiguous":
                        blocked.append(slot)
                        continue
                elif latest is None and failed_only:
                    continue  # untouched slots are start() scope, not retry
                # Attempt numbering stays scoped to (order, task_type) like
                # GenerationService: uniq_generation_attempt holds for FULL
                # jobs too, which also makes (order, slot_key, attempt)
                # unique per the partial constraint.
                attempt = (
                    GenerationJob.objects.filter(
                        order=locked,
                        task_type=GenerationJob.TaskType.FULL,
                    ).aggregate(max_attempt=Max("attempt"))["max_attempt"]
                    or 0
                ) + 1
                new_jobs.append(
                    GenerationJob.objects.create(
                        order=locked,
                        task_type=GenerationJob.TaskType.FULL,
                        status=GenerationJob.Status.RUNNING,
                        attempt=attempt,
                        slot_key=slot,
                        provider=self.provider.name,
                        input_metadata={
                            "product": locked.product.code,
                            "style": locked.style.code,
                            "photo_ids": photo_ids,
                            "source_preview_id": preview.pk,
                            "slot_key": slot,
                            "emotion": slot,
                        },
                        started_at=timezone.now(),
                    )
                )
            return new_jobs, blocked

    def _run_jobs(self, *, jobs) -> None:
        # One slot's failure must not abort the others; each job records
        # its own outcome.
        for job in jobs:
            self._run_slot_job(job=job)

    def _run_slot_job(self, *, job: GenerationJob) -> None:
        try:
            request = self._build_slot_request(job=job)
            result = self.provider.generate_preview(request)
            if not result.content:
                raise FullProductionError("Image provider returned empty content")
            self._complete_slot(job=job, result=result)
        except Exception as exc:
            self._fail_slot(job=job, exc=exc)

    def _build_slot_request(self, *, job: GenerationJob) -> ImageGenerationRequest:
        order = (
            Order.objects.select_related("product", "style")
            .prefetch_related("photos")
            .get(pk=job.order_id)
        )
        meta = job.input_metadata
        slot_key = meta["slot_key"]
        product_config = order.product.config or {}
        style_config = order.style.config or {}

        prompt_parts = [
            str(
                product_config.get("generation_prompt")
                or "Create a personalized sticker based on the reference images."
            ),
            str(style_config.get("prompt") or f"Use the {order.style.name} style."),
            f"Emotion: {slot_key}",
        ]
        if order.customer_notes.strip():
            prompt_parts.append(f"Customer notes: {order.customer_notes.strip()}")
        if order.operator_notes.strip():
            prompt_parts.append(f"Operator notes: {order.operator_notes.strip()}")

        # Identity lock: the customer-approved preview is the production
        # reference and always comes first, before customer photos.
        preview = GeneratedAsset.objects.get(pk=meta["source_preview_id"])
        with self.storage.open(preview.storage_key, "rb") as source:
            references = [
                ReferenceImage(
                    filename=f"approved-preview-{preview.pk}.png",
                    mime_type=preview.mime_type or "image/png",
                    content=source.read(),
                )
            ]
        photos = order.photos.exclude(status=OrderPhoto.Status.REJECTED).order_by(
            "created_at", "pk"
        )
        for photo in photos:
            with self.storage.open(photo.storage_key, "rb") as source:
                references.append(
                    ReferenceImage(
                        filename=photo.original_filename or f"photo-{photo.pk}.jpg",
                        mime_type=photo.mime_type or "image/jpeg",
                        content=source.read(),
                    )
                )

        return ImageGenerationRequest(
            prompt="\n".join(part for part in prompt_parts if part),
            reference_images=references,
            metadata={
                "order_id": order.pk,
                "job_id": job.pk,
                "task_type": GenerationJob.TaskType.FULL,
                "slot_key": slot_key,
                "emotion": slot_key,
                "product": order.product.code,
                "style": order.style.code,
            },
        )

    @transaction.atomic
    def _complete_slot(self, *, job: GenerationJob, result) -> GeneratedAsset:
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            raise FullProductionError(f"Job #{locked_job.pk} is not running")
        locked_order = Order.objects.select_for_update().get(pk=locked_job.order_id)

        storage_key = (
            f"generated/order-{locked_order.pk}/final/"
            f"slot-{locked_job.slot_key}-job-{locked_job.pk}-{uuid4().hex}.png"
        )
        self.storage.save(storage_key, BytesIO(result.content))
        asset = GeneratedAsset.objects.create(
            order=locked_order,
            job=locked_job,
            kind=GeneratedAsset.Kind.FINAL,
            slot_key=locked_job.slot_key,
            storage_key=storage_key,
            mime_type=result.mime_type or "image/png",
            size_bytes=len(result.content),
            metadata={"emotion": locked_job.slot_key},
        )
        locked_job.status = GenerationJob.Status.SUCCEEDED
        locked_job.output_metadata = {"asset_id": asset.pk}
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "output_metadata", "finished_at", "updated_at"]
        )
        return asset

    @transaction.atomic
    def _fail_slot(self, *, job: GenerationJob, exc: Exception) -> None:
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            return
        locked_job.status = GenerationJob.Status.FAILED
        locked_job.error = str(exc)[:4000]
        locked_job.output_metadata = {
            "failure_class": classify_provider_failure(self.provider, exc)
        }
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "error", "output_metadata", "finished_at", "updated_at"]
        )

    @transaction.atomic
    def _finalize(self, *, order: Order) -> list[SlotState]:
        locked = Order.objects.select_for_update().get(pk=order.pk)
        slots = self.expected_slots(locked)
        produced = set(
            locked.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)
            .values_list("slot_key", flat=True)
        )
        if (
            locked.status == Order.Status.PACK_GENERATING
            and all(slot in produced for slot in slots)
        ):
            # QC-ready state; the QC flow itself is DRF-2052 scope.
            OrderStateService.transition(
                order=locked,
                to_status=Order.Status.QUALITY_CONTROL,
            )
        return self.production_plan(locked)
