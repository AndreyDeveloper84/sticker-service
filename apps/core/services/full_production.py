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
    describe_provider_failure,
)
from apps.core.models import GeneratedAsset, GenerationJob, Order, OrderPhoto, Payment
from apps.core.services.generation_prompts import (
    FRAMING_CLAUSE,
    FULL_DEFAULT_PROMPT,
    SAFE_FOR_WORK_CLAUSE,
    custom_phrase_for_slot,
    emotion_label,
    is_custom_slot,
    render_caption,
    render_expression,
    render_reference_roles,
)
from apps.core.services.channel_order_flow import (
    order_emotion_codes,
    product_emotion_options,
)
from apps.core.services.budget import BudgetGuard, BudgetOverride
from apps.core.services import generation_cost, generation_queue
from apps.core.services.generation import ALREADY_RUNNING_MESSAGE, QUEUED_MESSAGE, reap_stale
from apps.core.services.order_state import OrderStateService
from apps.core.storage import LocalMediaStorage


# Budget action names per _prepare mode (event payload / decision).
_BUDGET_ACTIONS = {"start": "full_start", "retry": "retry", "regenerate": "regenerate", "force": "force_retry"}
# _prepare mode → cost snapshot mode (DRF-2111 D4: regeneration marker).
_COST_MODES = {
    "start": generation_cost.MODE_INITIAL,
    "retry": generation_cost.MODE_RETRY_FAILED,
    "regenerate": generation_cost.MODE_REGENERATE,
    "force": generation_cost.MODE_FORCE_RETRY,
}


class FullProductionError(ValueError):
    pass


@dataclass(frozen=True)
class SlotState:
    """Stable production-plan slot derived from FULL GenerationJob rows.

    asset_id is the CURRENT asset of the slot: the FINAL asset produced by
    the slot's latest SUCCEEDED FULL attempt. Older FINAL assets of the
    slot are retained for audit but are not current.
    """

    slot_key: str
    emotion: str
    status: str  # "pending" | "queued" | "running" | "succeeded" | "failed"
    asset_id: int | None
    attempts: int
    retryable: bool


class FullProductionService:
    """Produces exactly the purchased volume after preview approval.

    One slot per selected emotion code; slot_key IS the emotion code, so
    re-entry maps onto the same slots and never duplicates successful
    work. Identity consistency comes from the customer-approved preview,
    which is the first reference image of every slot job.

    Cost safety: ambiguous failures (post-submit provider timeout, stale
    RUNNING jobs) are failed closed — never auto-regenerated. The only way
    past a blocked slot is the explicit operator action force_retry_slot().

    Async C-2 — lazy chain. An entry point creates exactly ONE PENDING slot
    job (the first candidate) and hands it to the executor
    (generation_queue). After a slot SUCCEEDS the worker calls
    continue_chain(): the same _prepare() under the order lock creates the
    next PENDING slot (Budget Guard per slot) — so an order never has more
    than one PENDING/RUNNING slot job, and any failed / ambiguous slot stops
    the chain (fail closed; retry / regenerate / force are operator actions).
    The whole remaining pack is checked against the budget upfront
    (BudgetGuard.enforce(planned=N)) before the first job exists.

    max_slots: how many slots this request may produce in total (chain
    length); None = the whole remaining pack (one click = the pack).
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

    @staticmethod
    def _jobs_by_slot(order: Order) -> dict[str, list[GenerationJob]]:
        jobs_by_slot: dict[str, list[GenerationJob]] = {}
        full_jobs = GenerationJob.objects.filter(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
        ).order_by("slot_key", "attempt")
        for job in full_jobs:
            jobs_by_slot.setdefault(job.slot_key, []).append(job)
        return jobs_by_slot

    def production_plan(self, order: Order) -> list[SlotState]:
        slots = self.expected_slots(order)
        jobs_by_slot = self._jobs_by_slot(order)
        assets_by_job = {
            asset.job_id: asset
            for asset in order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)
        }

        plan = []
        for slot in slots:
            slot_jobs = jobs_by_slot.get(slot, [])
            latest = slot_jobs[-1] if slot_jobs else None
            latest_succeeded = next(
                (
                    job
                    for job in reversed(slot_jobs)
                    if job.status == GenerationJob.Status.SUCCEEDED
                ),
                None,
            )
            asset = assets_by_job.get(latest_succeeded.pk) if latest_succeeded else None
            asset_id = asset.pk if asset else None
            attempts = len(slot_jobs)
            if latest is None:
                plan.append(SlotState(slot, slot, "pending", None, 0, True))
            elif latest.status == GenerationJob.Status.RUNNING:
                plan.append(SlotState(slot, slot, "running", asset_id, attempts, False))
            elif latest.status == GenerationJob.Status.PENDING:
                # queued for the worker (async C-2); not retryable while waiting
                plan.append(SlotState(slot, slot, "queued", asset_id, attempts, False))
            elif latest.status == GenerationJob.Status.FAILED:
                failure_class = (latest.output_metadata or {}).get("failure_class")
                plan.append(
                    SlotState(
                        slot, slot, "failed", asset_id, attempts, failure_class != "ambiguous"
                    )
                )
            elif latest.status == GenerationJob.Status.SUCCEEDED:
                plan.append(SlotState(slot, slot, "succeeded", asset_id, attempts, False))
            else:
                plan.append(SlotState(slot, slot, "pending", asset_id, attempts, True))
        return plan

    # ---------------------------------------------------------- entries

    def start(
        self, *, order: Order, max_slots: int | None = None, budget_override: BudgetOverride | None = None
    ) -> list[SlotState]:
        """Run full production; safe to re-enter (no duplicate assets).

        Pending slots are started one after another (lazy chain, up to
        max_slots). Succeeded slots are preserved. Failed retryable slots
        are NOT auto-retried here — they go through retry_failed();
        ambiguous-blocked slots stay blocked until force_retry_slot().
        """
        jobs, _blocked = self._prepare(
            order=order, allow_entry=True, mode="start", max_slots=max_slots,
            budget_override=budget_override,
        )
        self._run_jobs(jobs=jobs)
        return self._finalize(order=order)

    def retry_failed(
        self, *, order: Order, max_slots: int | None = None, budget_override: BudgetOverride | None = None
    ) -> list[SlotState]:
        """Selective retry: failed retryable slots only, never succeeded ones.

        Slots whose latest failure was classified "ambiguous" (post-submit
        provider timeout) are skipped — a blind retry could duplicate
        billable generation — and reported via FullProductionError after
        the other slots have been processed.
        """
        jobs, blocked = self._prepare(
            order=order, allow_entry=False, mode="retry", max_slots=max_slots,
            budget_override=budget_override,
        )
        self._run_jobs(jobs=jobs)
        plan = self._finalize(order=order)
        if blocked:
            raise FullProductionError(
                "Slots need manual intervention (ambiguous provider failure, "
                f"no blind retry): {', '.join(blocked)}"
            )
        return plan

    def regenerate_slots(
        self, *, order: Order, slot_keys, max_slots: int | None = None,
        budget_override: BudgetOverride | None = None,
    ) -> list[SlotState]:
        """Regenerate explicitly requested slots (QC FAIL path, DRF-2052).

        PACK_GENERATING only. Every slot_key must be in expected_slots.
        A new FULL attempt is created per requested slot even when a FINAL
        asset already exists: the new asset replaces the old one as the
        slot's current asset; the old row is retained for audit. Other
        slots are untouched. Ambiguous-blocked slots are skipped and
        reported via FullProductionError after the rest are processed.
        """
        jobs, blocked = self._prepare(
            order=order,
            allow_entry=False,
            mode="regenerate",
            slot_keys=slot_keys,
            max_slots=max_slots,
            budget_override=budget_override,
        )
        self._run_jobs(jobs=jobs)
        plan = self._finalize(order=order)
        if blocked:
            raise FullProductionError(
                "Slots are blocked (ambiguous provider failure); use "
                f"force_retry_slot after manual verification: {', '.join(blocked)}"
            )
        return plan

    def force_retry_slot(
        self, *, order: Order, slot_key: str, budget_override: BudgetOverride | None = None
    ) -> list[SlotState]:
        """Explicit operator override for one ambiguous-blocked slot.

        PACK_GENERATING only; the slot's latest attempt must be FAILED
        with failure_class "ambiguous" (a stale RUNNING job is marked as
        such first). Use only after verifying the generation did not
        complete / was not billed — otherwise a duplicate billable
        generation is possible. On success the slot follows normal rules.
        """
        jobs, _blocked = self._prepare(
            order=order,
            allow_entry=False,
            mode="force",
            slot_keys=[slot_key],
            max_slots=1,
            budget_override=budget_override,
        )
        self._run_jobs(jobs=jobs)
        return self._finalize(order=order)

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

    def _lock_for_production(self, order: Order, *, allow_entry: bool) -> tuple:
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
                raise FullProductionError(f"Order #{locked.pk} has no confirmed payment")
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
        return locked, preview

    @staticmethod
    def _failure_class(job: GenerationJob) -> str | None:
        return (job.output_metadata or {}).get("failure_class")

    def _prepare(
        self,
        *,
        order: Order,
        allow_entry: bool,
        mode: str,
        slot_keys=None,
        max_slots: int | None,
        budget_override: BudgetOverride | None = None,
    ):
        """Create RUNNING attempts for slots that need (re)generation.

        Returns (new jobs, blocked slot keys). Blocked slots ended in an
        "ambiguous" state (post-submit timeout or stale RUNNING) and are
        never auto-regenerated — only force_retry_slot() may unblock them.
        """
        with transaction.atomic():
            locked, preview = self._lock_for_production(order, allow_entry=allow_entry)
            # Stale RUNNING (worker gone ≥ 15 min) → ambiguous; a FRESH
            # PENDING / RUNNING slot means the worker is on it: refuse the
            # re-entry instead of burning the slot (async C-2).
            reap_stale(locked)
            active = (
                GenerationJob.objects.select_for_update()
                .filter(
                    order=locked,
                    task_type=GenerationJob.TaskType.FULL,
                    status__in=[GenerationJob.Status.PENDING, GenerationJob.Status.RUNNING],
                )
                .order_by("pk")
                .first()
            )
            if active is not None:
                if active.status == GenerationJob.Status.PENDING:
                    raise FullProductionError(QUEUED_MESSAGE.format(job_id=active.pk))
                raise FullProductionError(ALREADY_RUNNING_MESSAGE)
            slots = self.expected_slots(locked)
            photo_ids = list(
                locked.photos.exclude(status=OrderPhoto.Status.REJECTED)
                .order_by("created_at", "pk")
                .values_list("id", flat=True)
            )
            jobs_by_slot = self._jobs_by_slot(locked)

            if mode in ("regenerate", "force"):
                requested = list(dict.fromkeys(str(key) for key in (slot_keys or [])))
                if not requested:
                    raise FullProductionError("No production slots requested")
                bad = [key for key in requested if key not in slots]
                if bad:
                    raise FullProductionError(
                        f"Unknown production slots for order #{locked.pk}: "
                        f"{', '.join(bad)}"
                    )
                candidates = requested
            else:
                candidates = slots

            # First pass: which slots need a new attempt (todo) and which are
            # blocked. Second: budget for the whole remaining pack, then ONE
            # PENDING job for the first todo slot; the chain continues from
            # the worker (continue_chain → _prepare again).
            todo = []
            blocked = []
            for slot in candidates:
                slot_jobs = jobs_by_slot.get(slot, [])
                latest = slot_jobs[-1] if slot_jobs else None

                if mode == "force":
                    if (
                        latest is None
                        or latest.status != GenerationJob.Status.FAILED
                        or self._failure_class(latest) != "ambiguous"
                    ):
                        raise FullProductionError(
                            f"Slot {slot} of order #{locked.pk} is not blocked; "
                            "force retry applies only to ambiguous-failed slots"
                        )
                elif latest is not None:
                    if latest.status == GenerationJob.Status.SUCCEEDED:
                        if mode != "regenerate":
                            continue  # preserve successful work, never regenerate
                    elif latest.status == GenerationJob.Status.FAILED:
                        if self._failure_class(latest) == "ambiguous":
                            blocked.append(slot)
                            continue
                        if mode == "start":
                            continue  # plain re-entry does not auto-retry failures
                    elif mode == "retry":
                        continue  # untouched slots are start() scope, not retry

                todo.append(slot)

            if max_slots is not None:
                todo = todo[:max_slots]
            if not todo:
                return [], blocked
            guard = BudgetGuard(override=budget_override)
            # Pilot Budget Guard (DRF-2086): inside the order lock, before the
            # billable attempt exists. The whole remaining chain is checked
            # upfront (planned=N) so a pack that cannot be afforded never
            # starts; the first slot is then checked on its own (per-slot
            # limits) and every later slot again in continue_chain. Raises →
            # rollback → no job, no provider call.
            if len(todo) > 1:
                guard.enforce(locked, GenerationJob.TaskType.FULL, action=_BUDGET_ACTIONS[mode], planned=len(todo))
            slot = todo[0]
            guard.enforce(locked, GenerationJob.TaskType.FULL, slot_key=slot, action=_BUDGET_ACTIONS[mode])
            chain = {
                "mode": mode,
                "requested_slots": list(slot_keys or []) if mode in ("regenerate", "force") else None,
                # slots this request may still produce after this one
                "remaining": None if max_slots is None else max_slots - 1,
                "auto_continue": mode != "force" and (max_slots is None or max_slots > 1),
            }
            job = self._create_attempt(
                locked, slot, preview, photo_ids,
                cost_mode=_COST_MODES[mode],
                qc_attempt=self._qc_retry_attempt(locked, slot) if mode == "regenerate" else None,
                chain=chain,
            )
            return [job], blocked

    @staticmethod
    def _qc_retry_attempt(locked: Order, slot: str):
        """QC attempt number when this regeneration was requested by a FAILED
        QC report (DRF-2052 retry_slots), else None."""
        from apps.core.services.qc import QcService

        report = QcService.latest_report(locked)
        if report is None:
            return None
        queued = {str(item.get("slot_key") or "") for item in (report.retry_slots or [])}
        return report.attempt if slot in queued else None

    def _create_attempt(
        self, locked: Order, slot: str, preview: GeneratedAsset, photo_ids: list,
        *, cost_mode: str = generation_cost.MODE_INITIAL, qc_attempt=None, chain: dict | None = None,
    ) -> GenerationJob:
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
        # PENDING = created, not yet picked up; started_at is set by
        # generation_queue.claim (PENDING → RUNNING) in the worker.
        return GenerationJob.objects.create(
            order=locked,
            task_type=GenerationJob.TaskType.FULL,
            status=GenerationJob.Status.PENDING,
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
                # DRF-2111: immutable price snapshot, same transaction as the
                # Budget Guard check, before the provider is called.
                generation_cost.COST_KEY: generation_cost.cost_snapshot(
                    provider=self.provider,
                    task_type=GenerationJob.TaskType.FULL,
                    mode=cost_mode,
                    qc_attempt=qc_attempt,
                ),
                "queue": {"requested_at": timezone.now().isoformat()},
                "chain": chain or {"mode": "start", "requested_slots": None, "remaining": 0, "auto_continue": False},
            },
        )

    def _run_jobs(self, *, jobs) -> None:
        # At most one job (the chain head); the executor runs it inline or
        # in the worker, which continues the chain itself.
        for job in jobs:
            generation_queue.dispatch(job, service=self)

    # ------------------------------------------------- worker side (C-2)

    def execute_claimed(self, job: GenerationJob) -> GenerationJob | None:
        """Worker side: ``job`` is a RUNNING (claimed) FULL slot. Calls the
        provider once, records the outcome, finalizes the order and, after a
        success, creates + dispatches the next slot of the chain. Returns the
        next job or None (chain finished or stopped)."""
        self._run_slot_job(job=job)
        order = Order.objects.get(pk=job.order_id)
        self._finalize(order=order)
        job.refresh_from_db()
        if job.status != GenerationJob.Status.SUCCEEDED:
            return None  # any failure (incl. ambiguous) stops the chain
        return self.continue_chain(order=order, job=job)

    def continue_chain(self, *, order: Order, job: GenerationJob) -> GenerationJob | None:
        """Next slot of the chain that ``job`` belongs to: the same _prepare()
        under the order lock (guards, Budget Guard per slot), one PENDING job,
        dispatched. None when nothing is left, the chain is exhausted
        (max_slots) or the mode does not chain (force)."""
        chain = dict((job.input_metadata or {}).get("chain") or {})
        if not chain.get("auto_continue"):
            return None
        remaining = chain.get("remaining")
        if remaining is not None and remaining <= 0:
            return None
        mode = str(chain.get("mode") or "start")
        try:
            jobs, _blocked = self._prepare(
                order=order,
                allow_entry=False,
                mode=mode,
                slot_keys=chain.get("requested_slots"),
                max_slots=remaining,
                # the operator's override (if any) applied to the first slot
                # only; later slots face the plain budget
                budget_override=None,
            )
        except (FullProductionError, Exception) as exc:  # noqa: BLE001 — chain stop is a recorded fact
            generation_queue.logger.warning(
                "generation.chain.stopped order=%s after_job=%s reason=%s", order.pk, job.pk, exc
            )
            return None
        if not jobs:
            return None
        self._run_jobs(jobs=jobs)
        return jobs[0]

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

        # DRF-2080: identity source = customer photos (first); the approved
        # preview is passed LAST and only as a style/character reference.
        # "generation_prompt" is the preview wording; FULL uses its own key.
        photos = list(
            order.photos.exclude(status=OrderPhoto.Status.REJECTED).order_by("created_at", "pk")
        )
        preview = GeneratedAsset.objects.get(pk=meta["source_preview_id"])
        # Custom-caption slot: the customer's phrase is rendered as a caption
        # instruction (never as an emotion "Expression:"); a custom slot
        # without a phrase fails closed before any billable call.
        phrase = custom_phrase_for_slot(order, slot_key)
        if is_custom_slot(slot_key):
            if not phrase:
                raise FullProductionError(
                    f"Order #{order.pk} slot {slot_key} has no customer phrase"
                )
            slot_line = render_caption(phrase)
            label = phrase
        else:
            slot_line = render_expression(order.product, slot_key)
            label = emotion_label(order.product, slot_key)
        prompt_parts = [
            str(product_config.get("full_generation_prompt") or FULL_DEFAULT_PROMPT),
            str(style_config.get("prompt") or f"Use the {order.style.name} style."),
            SAFE_FOR_WORK_CLAUSE,
            FRAMING_CLAUSE,
            slot_line,
            render_reference_roles(photo_count=len(photos), has_preview=True),
        ]
        if order.customer_notes.strip():
            prompt_parts.append(f"Customer notes: {order.customer_notes.strip()}")
        if order.operator_notes.strip():
            prompt_parts.append(f"Operator notes: {order.operator_notes.strip()}")
        prompt = "\n".join(part for part in prompt_parts if part)

        references = []
        reference_order = []
        for photo in photos:
            with self.storage.open(photo.storage_key, "rb") as source:
                references.append(
                    ReferenceImage(
                        filename=photo.original_filename or f"photo-{photo.pk}.jpg",
                        mime_type=photo.mime_type or "image/jpeg",
                        content=source.read(),
                    )
                )
            reference_order.append(f"photo:{photo.pk}")
        with self.storage.open(preview.storage_key, "rb") as source:
            references.append(
                ReferenceImage(
                    filename=f"approved-preview-{preview.pk}.png",
                    mime_type=preview.mime_type or "image/png",
                    content=source.read(),
                )
            )
        reference_order.append(f"preview:{preview.pk}")

        # Live evidence: persist exactly what the model received.
        job.input_metadata = {
            **(job.input_metadata or {}),
            "emotion_label": label,
            "prompt": prompt,
            "reference_order": reference_order,
        }
        job.save(update_fields=["input_metadata", "updated_at"])

        return ImageGenerationRequest(
            prompt=prompt,
            reference_images=references,
            metadata={
                "order_id": order.pk,
                "job_id": job.pk,
                "task_type": GenerationJob.TaskType.FULL,
                "slot_key": slot_key,
                "emotion": slot_key,
                "emotion_label": label,
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
        # Keep provider metadata (model, usage) next to the asset id, as
        # GenerationService._complete does — pilot metrics read FULL cost here;
        # the worker facts written at claim (picked_at, …) are preserved.
        locked_job.output_metadata = {**(locked_job.output_metadata or {}), "asset_id": asset.pk, **(result.metadata or {})}
        generation_cost.apply_success(locked_job, result.metadata)
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "output_metadata", "input_metadata", "finished_at", "updated_at"]
        )
        return asset

    @transaction.atomic
    def _fail_slot(self, *, job: GenerationJob, exc: Exception) -> None:
        locked_job = GenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked_job.status != GenerationJob.Status.RUNNING:
            return
        locked_job.status = GenerationJob.Status.FAILED
        locked_job.error = str(exc)[:4000]
        failure = describe_provider_failure(self.provider, exc)
        locked_job.output_metadata = {**(locked_job.output_metadata or {}), **failure}
        generation_cost.apply_failure(locked_job, failure)
        locked_job.finished_at = timezone.now()
        locked_job.save(
            update_fields=["status", "error", "output_metadata", "input_metadata", "finished_at", "updated_at"]
        )

    @transaction.atomic
    def _finalize(self, *, order: Order) -> list[SlotState]:
        locked = Order.objects.select_for_update().get(pk=order.pk)
        slots = self.expected_slots(locked)
        latest_by_slot = {}
        for slot, slot_jobs in self._jobs_by_slot(locked).items():
            if slot_jobs:
                latest_by_slot[slot] = slot_jobs[-1]
        # Complete only when every required slot's LATEST attempt succeeded.
        # A stale FINAL asset from an earlier attempt (e.g. QC-rejected,
        # then failed regeneration) must not bounce the order back to QC.
        complete = all(
            slot in latest_by_slot
            and latest_by_slot[slot].status == GenerationJob.Status.SUCCEEDED
            for slot in slots
        )
        if complete and locked.status == Order.Status.PACK_GENERATING:
            # QC-ready state; the QC flow itself is DRF-2052 scope.
            OrderStateService.transition(
                order=locked,
                to_status=Order.Status.QUALITY_CONTROL,
            )
        return self.production_plan(locked)
