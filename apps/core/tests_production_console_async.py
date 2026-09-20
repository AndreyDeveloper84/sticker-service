"""D-1: the Production Console with a background generation worker.

The click creates the PENDING job and returns; the order card is the waiting
screen. These tests drive the console through the RecordingExecutor (the
job stays PENDING until ``run_all``) and check: the card while queued /
running, the hidden and server-refused actions, the outcome line, the
stale-RUNNING reap on GET, «Снять из очереди» and the worker indicator.
The inline mode (default) is covered by the existing console suites, which
run unchanged.
"""

import html as html_lib
import re
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.console_generation import WorkerHealth
from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderPhoto,
    Product,
    Revision,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.generation import GenerationService
from apps.core.services.generation_queue import QUEUE_LOST, RecordingExecutor, claim, use_executor
from apps.core.services.order_state import OrderStateService
from apps.core.services.preview_feedback import PreviewFeedbackService
from apps.core.storage import LocalMediaStorage

META_REFRESH = 'http-equiv="refresh"'


class FakeProvider:
    name = "fake"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return ImageGenerationResult(
            content=f"image-{len(self.requests)}".encode(), mime_type="image/png", metadata={"fake": True}
        )


class AsyncConsoleCase(TestCase):
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
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="100"
        )
        product = Product.objects.create(
            code="stickers", name="Sticker Pack", config={"generation_prompt": "Generate preview"}
        )
        style = Style.objects.create(code="comic", name="Comic", config={"prompt": "Comic style"})
        self.order = Order.objects.create(
            user=user, channel_identity=identity, product=product, style=style, status=Order.Status.PAID
        )
        self.storage.save("orders/source.jpg", BytesIO(b"source"))
        OrderPhoto.objects.create(
            order=self.order, storage_key="orders/source.jpg", original_filename="source.jpg",
            mime_type="image/jpeg", size_bytes=6,
        )
        self.provider = FakeProvider()
        self.service = GenerationService(provider=self.provider, storage=self.storage)
        patcher = mock.patch.object(ProductionOrderAdmin, "get_generation_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.executor = RecordingExecutor()
        executor_cm = use_executor(self.executor)
        executor_cm.__enter__()
        self.addCleanup(executor_cm.__exit__, None, None, None)
        self.change_url = reverse("admin:core_order_change", args=[self.order.pk])

    # ---------------------------------------------------------- helpers

    def url(self, name, *args):
        return reverse(f"admin:{name}", args=[self.order.pk, *args])

    def page(self):
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def block(self, html, name):
        start = html.index(f"field-{name}")
        end = html.find("field-", start + 1)
        text = html[start:end if end > 0 else None]
        return html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))

    def messages(self, response):
        return [
            html_lib.unescape(re.sub(r"<[^>]+>", "", m).strip())
            for m in re.findall(r'<li class="(?:error|success|warning|info)">(.*?)</li>', response.content.decode(), re.S)
        ]

    def queue_preview(self):
        response = self.client.post(self.url("core_order_generate_preview"), follow=True)
        job = GenerationJob.objects.get(order=self.order, task_type=GenerationJob.TaskType.PREVIEW)
        return response, job

    def run_worker(self):
        self.executor.run_all(service=self.service)

    def latest_preview(self):
        return GeneratedAsset.objects.filter(order=self.order, kind=GeneratedAsset.Kind.PREVIEW).latest("pk")


class QueuedCardTests(AsyncConsoleCase):
    def test_click_queues_and_the_card_becomes_the_waiting_screen(self):
        response, job = self.queue_preview()
        self.assertEqual(job.status, GenerationJob.Status.PENDING)
        self.assertEqual(self.executor.dispatched, [job.pk])
        self.assertEqual(self.provider.requests, [])
        self.assertIn(f"Поставлено в очередь: превью, попытка 1 (job #{job.pk})", " ".join(self.messages(response)))

        html = self.page()
        self.assertIn(META_REFRESH, html)
        step = self.block(html, "next_step")
        self.assertIn(f"В очереди: превью (job #{job.pk})", step)
        self.assertIn("Страница обновляется каждые 10 с", step)
        self.assertNotIn("Сгенерировать превью", step)
        for name in ("core_order_generate_preview", "core_order_regenerate_preview", "core_order_generate_revision"):
            self.assertNotIn(self.url(name), html)
        self.assertIn("Пока идёт генерация, дополнительных действий нет.", html)
        self.assertIn("в очереди", self.block(html, "generation_history"))

    def test_running_job_shows_elapsed_seconds(self):
        _response, job = self.queue_preview()
        claim(job.pk)
        GenerationJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(seconds=42))
        step = self.block(self.page(), "next_step")
        self.assertRegex(step, rf"Генерируется превью… job #{job.pk} · 4[2-9] с")

    def test_worker_result_replaces_the_waiting_screen(self):
        _response, job = self.queue_preview()
        self.run_worker()
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(len(self.provider.requests), 1)
        html = self.page()
        self.assertNotIn(META_REFRESH, html)
        step = self.block(html, "next_step")
        asset = self.latest_preview()
        self.assertIn(f"Одобрить превью #{asset.pk}", step)
        self.assertIn(f"Последняя генерация: превью #{asset.pk} готово", step)
        self.assertIn(f"(job #{job.pk})", step)
        self.assertIn(self.url("core_order_regenerate_preview"), html)  # secondary actions are back

    def test_double_click_queues_exactly_once(self):
        _response, job = self.queue_preview()
        response = self.client.post(self.url("core_order_generate_preview"), follow=True)
        self.assertIn(f"В очереди: превью (job #{job.pk})", " ".join(self.messages(response)))
        self.assertEqual(self.executor.dispatched, [job.pk])
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_GENERATING)

    def test_actions_are_refused_on_the_server_while_queued(self):
        _response, job = self.queue_preview()
        asset = GeneratedAsset.objects.create(
            order=self.order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key="x", mime_type="image/png",
            size_bytes=1,
        )
        for name, args in (
            ("core_order_regenerate_preview", ()),
            ("core_order_generate_revision", ()),
            ("core_order_approve_preview", (asset.pk,)),
            ("core_order_deliver_preview", ()),
            ("core_order_start_full_production", ()),
            ("core_order_retry_failed_production", ()),
            ("core_order_regenerate_slots", ()),
            ("core_order_qc_start", ()),
        ):
            with self.subTest(name=name):
                for method in ("get", "post"):
                    response = getattr(self.client, method)(self.url(name, *args), follow=True)
                    self.assertEqual(response.redirect_chain[-1][0], self.change_url)
                    self.assertIn("дождитесь результата, действия пока недоступны", " ".join(self.messages(response)))
        self.assertEqual(self.executor.dispatched, [job.pk])
        self.assertEqual(GenerationJob.objects.filter(order=self.order).count(), 1)
        self.assertNotIn("internal_approved", asset.metadata)

    def test_provider_failure_is_reported_on_the_card(self):
        self.provider.fail = True
        _response, job = self.queue_preview()
        self.run_worker()
        html = self.page()
        self.assertNotIn(META_REFRESH, html)
        step = self.block(html, "next_step")
        # PREVIEW_GENERATING with a failed attempt: the main button starts again
        self.assertIn("Сгенерировать превью", step)
        self.assertIn(f"Последняя генерация: job #{job.pk} (превью) — ошибка: provider unavailable", step)
        self.assertIn(self.url("core_order_generate_preview"), html)

    def test_stale_running_is_reaped_on_the_card_get(self):
        _response, job = self.queue_preview()
        claim(job.pk)
        GenerationJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(minutes=16))
        html = self.page()
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], "ambiguous")
        self.assertNotIn(META_REFRESH, html)
        step = self.block(html, "next_step")
        self.assertIn("неоднозначно", step)
        self.assertIn("возможно, вызов был оплачен", step)
        self.assertIn(self.url("core_order_generate_preview"), html)  # the operator can go on
        self.assertEqual(self.provider.requests, [])

    def test_revision_queued_then_next_step_is_the_new_preview(self):
        self.service.request_preview(order=self.order)
        self.run_worker()
        source = self.latest_preview()
        source.metadata = {**(source.metadata or {}), "internal_approved": True,
                           "deliveries": [{"status": "sent", "channel": "telegram", "message_id": "1"}]}
        source.save(update_fields=["metadata", "updated_at"])
        self.order.refresh_from_db()
        OrderStateService.transition(order=self.order, to_status=Order.Status.PREVIEW_REVIEW)
        PreviewFeedbackService.request_revision(order=self.order, category=Revision.Category.FACE, customer_text="closer")
        self.order.refresh_from_db()

        response = self.client.post(self.url("core_order_generate_revision"), follow=True)
        job = GenerationJob.objects.get(order=self.order, task_type=GenerationJob.TaskType.REVISION)
        self.assertEqual(job.status, GenerationJob.Status.PENDING)
        self.assertIn("Поставлено в очередь: правка", " ".join(self.messages(response)))
        step = self.block(self.page(), "next_step")
        self.assertIn(f"В очереди: правка (job #{job.pk})", step)
        self.assertNotIn("Одобрить", self.block(self.page(), "preview_assets"))

        self.run_worker()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        revised = self.latest_preview()
        self.assertNotEqual(revised.pk, source.pk)
        step = self.block(self.page(), "next_step")
        self.assertIn(f"Одобрить превью #{revised.pk}", step)
        self.assertNotIn("Отправить превью клиенту", step)


class DequeueTests(AsyncConsoleCase):
    def _age(self, job, minutes):
        GenerationJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))

    def test_fresh_pending_has_no_dequeue_offer_and_refuses(self):
        _response, job = self.queue_preview()
        self._age(job, 2)
        html = self.page()
        self.assertNotIn(self.url("core_order_dequeue_job", job.pk), html)
        response = self.client.post(self.url("core_order_dequeue_job", job.pk), follow=True)
        self.assertIn("ждёт worker'а меньше 5 мин", " ".join(self.messages(response)))
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.PENDING)

    def test_waiting_pending_can_be_dequeued(self):
        _response, job = self.queue_preview()
        self._age(job, 6)
        html = self.page()
        step = self.block(html, "next_step")
        self.assertIn(f"Job #{job.pk} ждёт worker'а 6 мин", step)
        dequeue_url = self.url("core_order_dequeue_job", job.pk)
        self.assertIn(dequeue_url, html)

        confirmation = self.client.get(dequeue_url).content.decode()
        self.assertIn(f"Снять из очереди job #{job.pk}", confirmation)
        self.assertIn("без вызова провайдера", confirmation)

        response = self.client.post(dequeue_url, follow=True)
        self.assertIn(f"Job #{job.pk} снят из очереди", " ".join(self.messages(response)))
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], QUEUE_LOST)
        self.assertEqual(self.provider.requests, [])
        # a late worker message is a no-op, and the operator can start again
        self.run_worker()
        self.assertEqual(self.provider.requests, [])
        html = self.page()
        self.assertNotIn(META_REFRESH, html)
        self.assertIn("снят из очереди — провайдер не вызывался", self.block(html, "next_step"))
        self.assertIn(self.url("core_order_generate_preview"), html)

    def test_dequeue_refuses_a_running_job(self):
        _response, job = self.queue_preview()
        self._age(job, 6)
        claim(job.pk)
        response = self.client.post(self.url("core_order_dequeue_job", job.pk), follow=True)
        self.assertIn("не в очереди", " ".join(self.messages(response)))
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.RUNNING)


class WorkerIndicatorTests(AsyncConsoleCase):
    def test_hidden_in_inline_mode(self):
        self.queue_preview()
        self.assertNotIn("worker:", self.block(self.page(), "next_step"))

    def test_shown_with_the_background_worker(self):
        self.queue_preview()
        with mock.patch("apps.core.production_console.worker_health", return_value=WorkerHealth(True, "worker: жив (heartbeat 12 с назад)")):
            self.assertIn("worker: жив (heartbeat 12 с назад)", self.block(self.page(), "next_step"))
        with mock.patch("apps.core.production_console.worker_health", return_value=WorkerHealth(False, "worker: не запущен (нет ни одного worker'а на очереди)")):
            self.assertIn("worker: не запущен", self.block(self.page(), "next_step"))

    def test_health_reads_rq_heartbeat_only_when_enabled(self):
        from apps.core import console_generation

        self.assertIsNone(console_generation.worker_health())
        with override_settings(GENERATION_WORKER_ENABLED=True):
            with mock.patch("apps.core.services.generation_queue.rq_connection", side_effect=ConnectionError("down")):
                health = console_generation.worker_health()
        self.assertFalse(health.alive)
        self.assertIn("нет связи с очередью", health.text)


class ConfirmationTextTests(AsyncConsoleCase):
    def test_wait_text_depends_on_the_executor_mode(self):
        content = self.client.get(self.url("core_order_generate_preview")).content.decode()
        self.assertIn("не закрывайте страницу", content)
        self.assertIn("onsubmit", content)  # double-submit guard
        with override_settings(GENERATION_WORKER_ENABLED=True):
            content = self.client.get(self.url("core_order_generate_preview")).content.decode()
        self.assertIn("выполняется в фоне", content)
        self.assertIn("страницу можно закрыть", content)
