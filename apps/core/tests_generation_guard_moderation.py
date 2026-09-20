"""DRF-2089: (A) no parallel billable preview/revision attempt while one is
RUNNING; (B) OpenAI moderation_blocked -> failure_class "moderation" with
stage/categories/request id, RU operator text, slot stays retryable;
(C) safe-for-work clause in every persisted prompt.
"""

import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openai import APIStatusError

from apps.core.console_text import humanize_error, job_error_text, moderation_text
from apps.core.image_providers import (
    ImageGenerationResult,
    OpenAIImageProvider,
    classify_provider_failure,
    describe_provider_failure,
    moderation_details,
)
from apps.core.models import (
    ChannelIdentity,
    GenerationJob,
    Order,
    OrderPhoto,
    Product,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.full_production import FullProductionService
from apps.core.services.generation import (
    ALREADY_RUNNING_MESSAGE,
    STALE_RUNNING_AFTER,
    GenerationError,
    GenerationService,
)
from apps.core.services.generation_prompts import FRAMING_CLAUSE, SAFE_FOR_WORK_CLAUSE
from apps.core.storage import LocalMediaStorage
from apps.core.tests_full_generation import FullProductionTestCase

MODERATION_BODY = {
    "message": "Your request was rejected as a result of our safety system.",
    "type": "invalid_request_error",
    "param": None,
    "code": "moderation_blocked",
    "moderation_stage": "output",
    "safety_violations": ["sexual"],
}


def moderation_error(*, envelope=False, request_id="req_abc123"):
    """openai.APIStatusError exactly as the SDK raises it (inner error body),
    or with the full {"error": ...} envelope when envelope=True."""
    request = httpx.Request("POST", "https://api.openai.com/v1/images/edits")
    headers = {"x-request-id": request_id} if request_id else {}
    response = httpx.Response(400, json={"error": MODERATION_BODY}, request=request, headers=headers)
    body = {"error": MODERATION_BODY} if envelope else MODERATION_BODY
    return APIStatusError(f"Error code: 400 - {body}", response=response, body=body)


def api_error(status, body):
    request = httpx.Request("POST", "https://api.openai.com/v1/images/edits")
    response = httpx.Response(status, json={"error": body}, request=request)
    return APIStatusError(f"Error code: {status} - {body}", response=response, body=body)


class ModeratingProvider:
    """Raises the SDK moderation error like OpenAIImageProvider would."""

    name = "openai"
    classify_failure = staticmethod(OpenAIImageProvider.classify_failure)

    def __init__(self, fail_slots=None):
        self.fail_slots = None if fail_slots is None else set(fail_slots)
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        slot = request.metadata.get("slot_key")
        if self.fail_slots is None or slot in self.fail_slots:
            raise moderation_error()
        return ImageGenerationResult(content=f"final-{slot}".encode())


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        return ImageGenerationResult(content=b"preview")


# ---------------------------------------------------------------- (B) classification

class ModerationClassificationTests(SimpleTestCase):
    def test_moderation_blocked_is_its_own_class(self):
        exc = moderation_error()
        self.assertEqual(OpenAIImageProvider.classify_failure(exc), "moderation")
        self.assertEqual(
            moderation_details(exc),
            {"moderation_stage": "output", "moderation_categories": ["sexual"], "request_id": "req_abc123"},
        )

    def test_envelope_body_and_missing_request_id_are_handled(self):
        exc = moderation_error(envelope=True, request_id="")
        self.assertEqual(OpenAIImageProvider.classify_failure(exc), "moderation")
        self.assertEqual(moderation_details(exc)["request_id"], "")
        self.assertEqual(moderation_details(exc)["moderation_categories"], ["sexual"])

    def test_other_400_stays_api_and_geo_stays_geo(self):
        bad = api_error(400, {"code": "invalid_image", "message": "bad image"})
        self.assertEqual(OpenAIImageProvider.classify_failure(bad), "api")
        self.assertIsNone(moderation_details(bad))
        geo = api_error(403, {"code": "unsupported_country_region_territory", "message": "unsupported_country_region_territory"})
        self.assertEqual(OpenAIImageProvider.classify_failure(geo), "geo")
        self.assertIsNone(moderation_details(RuntimeError("x")))

    def test_describe_provider_failure_carries_moderation_facts(self):
        provider = ModeratingProvider()
        self.assertEqual(
            describe_provider_failure(provider, moderation_error()),
            {
                "failure_class": "moderation",
                "moderation_stage": "output",
                "moderation_categories": ["sexual"],
                "request_id": "req_abc123",
            },
        )
        self.assertEqual(describe_provider_failure(provider, RuntimeError("boom")), {"failure_class": "ambiguous"})
        self.assertEqual(classify_provider_failure(FakeProvider(), RuntimeError("boom")), "unknown")

    def test_moderation_text_in_russian(self):
        text = moderation_text(
            {"moderation_stage": "output", "moderation_categories": ["sexual"], "request_id": "req_abc123"}
        )
        self.assertEqual(
            text,
            "Провайдер отклонил результат генерации модерацией (категория: sexual, стадия: output). "
            "Попробуйте ещё раз или запросите у клиента другое фото (лицо и плечи, нейтральная одежда). "
            "Request ID: req_abc123.",
        )
        self.assertNotIn("Request ID", moderation_text({"moderation_stage": "input"}))
        self.assertIn("категория: не указана", moderation_text({}))

    def test_humanize_error_uses_structured_failure_then_text_fallback(self):
        error = GenerationError("Error code: 400 - {...}")
        error.failure = {"failure_class": "moderation", "moderation_stage": "output", "moderation_categories": ["sexual"], "request_id": "req_1"}
        self.assertIn("модерацией (категория: sexual, стадия: output)", humanize_error(error))
        self.assertIn("Request ID: req_1", humanize_error(error))
        # Raw text without structure (e.g. legacy job.error) still gets RU.
        self.assertTrue(humanize_error(GenerationError(str(moderation_error()))).startswith("Провайдер отклонил"))
        self.assertEqual(humanize_error(GenerationError(ALREADY_RUNNING_MESSAGE)), ALREADY_RUNNING_MESSAGE + ".")


# ---------------------------------------------------------------- (A) guard + (B)/(C) preview

class PreviewGuardAndModerationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="telegram", external_user_id="g-1")
        product = Product.objects.create(code="stickers", name="Stickers", config={"generation_prompt": "Make preview"})
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Preserve likeness"})
        self.order = Order.objects.create(
            user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID
        )
        key = f"orders/{self.order.pk}/reference.jpg"
        self.storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(
            order=self.order, storage_key=key, original_filename="reference.jpg", mime_type="image/jpeg", size_bytes=12
        )

    def _running_job(self, task_type=GenerationJob.TaskType.PREVIEW, age=timedelta(seconds=10)):
        job = GenerationJob.objects.create(
            order=self.order,
            task_type=task_type,
            status=GenerationJob.Status.RUNNING,
            attempt=1,
            provider="fake",
            started_at=timezone.now() - age,
        )
        return job

    def test_second_attempt_while_running_is_rejected_without_job_or_provider_call(self):
        self.order.status = Order.Status.PREVIEW_GENERATING
        self.order.save(update_fields=["status"])
        running = self._running_job()
        provider = FakeProvider()
        with self.assertRaises(GenerationError) as ctx:
            GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        self.assertEqual(str(ctx.exception), ALREADY_RUNNING_MESSAGE)
        self.assertEqual(provider.requests, [])
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 1)
        running.refresh_from_db()
        self.assertEqual(running.status, GenerationJob.Status.RUNNING)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)

    def test_guard_applies_from_paid_too_and_does_not_transition(self):
        self._running_job()
        with self.assertRaises(GenerationError):
            GenerationService(provider=FakeProvider(), storage=self.storage).generate_preview(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)  # atomic: transition rolled back

    def test_attempt_allowed_after_running_job_finished(self):
        self.order.status = Order.Status.PREVIEW_GENERATING
        self.order.save(update_fields=["status"])
        job = self._running_job()
        job.status = GenerationJob.Status.FAILED
        job.save(update_fields=["status"])
        provider = FakeProvider()
        asset = GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(asset.job.attempt, 2)

    def test_guard_is_per_task_type(self):
        # A RUNNING preview job does not block a revision job (different task_type)
        # — the state machine already separates the two flows.
        self._running_job(task_type=GenerationJob.TaskType.REVISION)
        provider = FakeProvider()
        GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        self.assertEqual(len(provider.requests), 1)

    def test_stale_running_job_is_failed_closed_and_new_attempt_proceeds(self):
        self.order.status = Order.Status.PREVIEW_GENERATING
        self.order.save(update_fields=["status"])
        stale = self._running_job(age=STALE_RUNNING_AFTER + timedelta(minutes=1))
        provider = FakeProvider()
        asset = GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        stale.refresh_from_db()
        self.assertEqual(stale.status, GenerationJob.Status.FAILED)
        self.assertEqual(stale.output_metadata["failure_class"], "ambiguous")
        self.assertIn("stale", stale.error)
        self.assertEqual(asset.job.attempt, 2)
        self.assertEqual(len(provider.requests), 1)

    def test_moderation_failure_records_metadata_and_raises_structured_error(self):
        provider = ModeratingProvider()
        with self.assertRaises(GenerationError) as ctx:
            GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        self.assertEqual(ctx.exception.failure["failure_class"], "moderation")
        self.assertEqual(ctx.exception.failure["moderation_categories"], ["sexual"])
        job = GenerationJob.objects.get(order=self.order)
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], "moderation")
        self.assertEqual(job.output_metadata["moderation_stage"], "output")
        self.assertEqual(job.output_metadata["moderation_categories"], ["sexual"])
        self.assertEqual(job.output_metadata["request_id"], "req_abc123")
        self.assertIn("moderation_blocked", job.error)  # raw text kept for audit
        self.assertTrue(job_error_text(job).startswith("Провайдер отклонил результат генерации модерацией"))
        self.assertIn("Request ID: req_abc123", job_error_text(job))
        self.assertIn("модерацией (категория: sexual, стадия: output)", humanize_error(ctx.exception))
        # A later attempt is allowed (the failed job is not RUNNING).
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)
        GenerationService(provider=FakeProvider(), storage=self.storage).generate_preview(order=self.order)

    def test_preview_prompt_persists_safe_for_work_clause(self):
        provider = FakeProvider()
        asset = GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.order)
        self.assertIn(SAFE_FOR_WORK_CLAUSE, provider.requests[0].prompt)
        self.assertIn(SAFE_FOR_WORK_CLAUSE, asset.job.input_metadata["prompt"])
        lines = provider.requests[0].prompt.split("\n")
        self.assertEqual(
            lines[:4], ["Make preview", "Preserve likeness", SAFE_FOR_WORK_CLAUSE, FRAMING_CLAUSE]
        )
        self.assertIn(FRAMING_CLAUSE, asset.job.input_metadata["prompt"])
        self.assertIn("nothing below the chest", asset.job.input_metadata["prompt"])


# ---------------------------------------------------------------- FULL: moderation retryable + SFW

class FullProductionModerationTests(FullProductionTestCase):
    def test_moderated_slot_is_failed_with_facts_and_stays_retryable(self):
        order, _preview = self._make_order()
        provider = ModeratingProvider(fail_slots={"bye"})
        service = FullProductionService(provider=provider, storage=self.storage)
        plan = service.start(order=order, max_slots=None)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "failed")
        self.assertTrue(bye.retryable)  # not "ambiguous" -> Retry Failed Slots applies
        job = self._full_jobs(order).get(slot_key="bye")
        self.assertEqual(job.output_metadata["failure_class"], "moderation")
        self.assertEqual(job.output_metadata["moderation_stage"], "output")
        self.assertEqual(job.output_metadata["moderation_categories"], ["sexual"])
        self.assertEqual(job.output_metadata["request_id"], "req_abc123")
        self.assertTrue(job_error_text(job).startswith("Провайдер отклонил результат генерации модерацией"))
        # Retry with a provider that now passes.
        provider.fail_slots = set()
        plan = service.retry_failed(order=order, max_slots=None)
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))

    def test_full_prompt_persists_safe_for_work_clause(self):
        order, _preview = self._make_order()
        provider = ModeratingProvider(fail_slots=set())
        FullProductionService(provider=provider, storage=self.storage).start(order=order, max_slots=1)
        job = self._full_jobs(order).first()
        self.assertIn(SAFE_FOR_WORK_CLAUSE, job.input_metadata["prompt"])
        self.assertIn(SAFE_FOR_WORK_CLAUSE, provider.requests[0].prompt)
        self.assertIn(FRAMING_CLAUSE, job.input_metadata["prompt"])
        lines = job.input_metadata["prompt"].split(chr(10))
        # framing sits right after the SFW clause, before the expression line
        self.assertEqual(lines.index(FRAMING_CLAUSE), lines.index(SAFE_FOR_WORK_CLAUSE) + 1)
        self.assertTrue(lines[lines.index(FRAMING_CLAUSE) + 1].startswith("Expression:"))


# ---------------------------------------------------------------- console surface

class ConsoleModerationTextTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()
        self.client.force_login(
            get_user_model().objects.create_superuser(username="op", email="op@example.com", password="pass")
        )
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel="telegram", external_user_id="c-1")
        product = Product.objects.create(code="stickers", name="Stickers", config={"generation_prompt": "Make preview"})
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Comic"})
        self.order = Order.objects.create(
            user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID
        )
        key = f"orders/{self.order.pk}/reference.jpg"
        self.storage.save(key, BytesIO(b"person-photo"))
        OrderPhoto.objects.create(
            order=self.order, storage_key=key, original_filename="reference.jpg", mime_type="image/jpeg", size_bytes=12
        )

    def _post_generate(self, provider):
        service = GenerationService(provider=provider, storage=self.storage)
        with patch.object(ProductionOrderAdmin, "get_generation_service", return_value=service):
            return self.client.post(
                reverse("admin:core_order_generate_preview", args=[self.order.pk]), follow=True
            )

    def test_moderation_shows_russian_message_and_history_line(self):
        response = self._post_generate(ModeratingProvider())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Провайдер отклонил результат генерации модерацией (категория: sexual, стадия: output)")
        self.assertContains(response, "запросите у клиента другое фото")
        self.assertContains(response, "Request ID: req_abc123")
        self.assertNotContains(response, "invalid_request_error")  # no raw JSON on the page

    def test_double_click_while_running_shows_russian_wait_message(self):
        self.order.status = Order.Status.PREVIEW_GENERATING
        self.order.save(update_fields=["status"])
        GenerationJob.objects.create(
            order=self.order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.RUNNING,
            attempt=1,
            provider="fake",
            started_at=timezone.now(),
        )
        provider = FakeProvider()
        response = self._post_generate(provider)
        # D-1: the console refuses before the service does (same guard,
        # console wording: the running attempt and «дождитесь»)
        self.assertContains(response, "Генерируется превью… job #")
        self.assertContains(response, "дождитесь результата, действия пока недоступны")
        self.assertEqual(provider.requests, [])
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 1)
