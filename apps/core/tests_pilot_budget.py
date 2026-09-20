"""DRF-2086 PR1: pilot budget limits, the console gate, cost figures and the
refund event. Generation services are untouched — every check is on the
console boundary, so a blocked action must create zero GenerationJobs.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderEvent,
    OrderPhoto,
    Payment,
    Product,
    Style,
    User,
)
from apps.core.pilot_metrics_console import EventTypeFilter
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.budget import BudgetConfigError, BudgetService, limit_value
from apps.core.services.full_production import FullProductionService
from apps.core.services.generation import GenerationService
from apps.core.services.pilot_metrics import PilotMetricsService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_qc import make_image

EMOTIONS = [{"code": "e0", "label": "E0"}, {"code": "e1", "label": "E1"}, {"code": "e2", "label": "E2"}]
PACK3 = {"kind": "pack", "quantity": 3, "emotion_count": 3, "emotions": EMOTIONS, "price_minor": 50000}
UNLIMITED = dict(
    PILOT_MAX_IMAGE_CALLS_PER_DAY=0,
    PILOT_MAX_IMAGE_CALLS_PER_MONTH=0,
    PILOT_MAX_IMAGE_CALLS_PER_ORDER=0,
    PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=0,
)


class StickerProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(
            content=make_image(), mime_type="image/png",
            metadata={"usage": {"input_tokens": 10, "output_tokens": 90, "total_tokens": 100}},
        )


class LimitParsingTests(TestCase):
    @override_settings(**UNLIMITED)
    def test_zero_means_unlimited(self):
        for name in UNLIMITED:
            self.assertIsNone(limit_value(name))

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=None, PILOT_MAX_IMAGE_CALLS_PER_ORDER=None,
                       PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=None, PILOT_MAX_IMAGE_CALLS_PER_MONTH=None)
    def test_absent_uses_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            for name in ("PILOT_MAX_IMAGE_CALLS_PER_DAY", "PILOT_MAX_IMAGE_CALLS_PER_MONTH"):
                self.assertIsNone(limit_value(name))
            self.assertEqual(limit_value("PILOT_MAX_IMAGE_CALLS_PER_ORDER"), 15)
            self.assertEqual(limit_value("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"), 3)

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=None)
    def test_env_is_read_when_setting_is_absent(self):
        with patch.dict("os.environ", {"PILOT_MAX_IMAGE_CALLS_PER_DAY": "7"}):
            self.assertEqual(limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY"), 7)
        # invalid value → fail closed (BudgetConfigError), never "unlimited"
        with patch.dict("os.environ", {"PILOT_MAX_IMAGE_CALLS_PER_DAY": "garbage"}):
            with self.assertRaisesMessage(BudgetConfigError, "PILOT_MAX_IMAGE_CALLS_PER_DAY"):
                limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY")
        with patch.dict("os.environ", {"PILOT_MAX_IMAGE_CALLS_PER_DAY": "-1"}):
            with self.assertRaises(BudgetConfigError):
                limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY")


class BudgetFixture(TestCase):
    """Order in PREVIEW_REVIEW with a confirmed payment and a customer-approved
    preview: every production action is otherwise allowed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()

        self.superuser = get_user_model().objects.create_superuser(
            username="root", email="root@example.com", password="pass"
        )
        self.operator = get_user_model().objects.create_user(username="op", password="pass", is_staff=True)
        self.operator.user_permissions.set(Permission.objects.filter(content_type__app_label="core"))
        self.client.force_login(self.superuser)

        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="900"
        )
        product = Product.objects.create(code="pack3", name="Pack 3", config=PACK3)
        style = Style.objects.create(code="comic", name="Comic")
        self.order = self._make_order(user, product, style)

        storage = self.storage
        patcher = patch.object(
            ProductionOrderAdmin, "get_full_production_service",
            lambda self_: FullProductionService(provider=StickerProvider(), storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            ProductionOrderAdmin, "get_generation_service",
            lambda self_: GenerationService(provider=StickerProvider(), storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_order(self, user, product, style, status=Order.Status.PREVIEW_REVIEW):
        order = Order.objects.create(
            user=user, channel_identity=self.identity, product=product, style=style, status=status,
            selection={"emotions": [e["code"] for e in EMOTIONS]},
        )
        photo_key = f"orders/{order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(order=order, storage_key=photo_key, original_filename="photo.jpg",
                                  mime_type="image/jpeg", size_bytes=5)
        Payment.objects.create(order=order, provider="telegram_stars", status=Payment.Status.CONFIRMED,
                               amount_minor=460, currency="XTR", external_payment_id=f"charge-{order.pk}",
                               confirmed_at=timezone.now())
        preview_job = GenerationJob.objects.create(
            order=order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.SUCCEEDED,
            attempt=1, provider="fake", started_at=timezone.now(), finished_at=timezone.now(),
            output_metadata={"usage": {"total_tokens": 3000}},
        )
        preview_key = f"generated/order-{order.pk}/preview/job-{preview_job.pk}.png"
        self.storage.save(preview_key, BytesIO(b"preview"))
        GeneratedAsset.objects.create(order=order, job=preview_job, kind=GeneratedAsset.Kind.PREVIEW,
                                      storage_key=preview_key, size_bytes=7,
                                      metadata={"internal_approved": True, "customer_approved": True})
        return order

    def _calls(self, count, *, when=None, order=None, task=GenerationJob.TaskType.FULL, slot_key="", status=None,
               ambiguous=False):
        target = order or self.order
        existing = GenerationJob.objects.filter(order=target, task_type=task).count()
        for index in range(count):
            GenerationJob.objects.create(
                order=target, task_type=task,
                status=status or GenerationJob.Status.FAILED, attempt=existing + index + 1, provider="fake",
                slot_key=slot_key, started_at=when or timezone.now(),
                output_metadata={"failure_class": "ambiguous"} if ambiguous else {},
            )

    def _post(self, url_name, *args, data=None):
        response = self.client.post(reverse(f"admin:{url_name}", args=args), data=data or {}, follow=True)
        self.assertEqual(response.status_code, 200)
        return [str(m) for m in response.context["messages"]]

    def _full_jobs(self):
        return GenerationJob.objects.filter(order=self.order, task_type=GenerationJob.TaskType.FULL)

    def _events(self, event_type):
        return list(OrderEvent.objects.filter(order=self.order, event_type=event_type))


@override_settings(**UNLIMITED)
class ConsoleBudgetGateTests(BudgetFixture):
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_day_limit_blocks_production_and_logs_event(self):
        self._calls(1)  # + the preview job from the fixture = 2 today
        messages = self._post("core_order_start_full_production", self.order.pk)

        self.assertEqual(messages, ["Лимит вызовов в день исчерпан: 2/2. Генерация не запущена."])
        self.assertEqual(self._full_jobs().filter(status=GenerationJob.Status.SUCCEEDED).count(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)
        (event,) = self._events(OrderEvent.BUDGET_BLOCKED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload, {"action": "full_start", "limit": "day", "slot_key": "", "used": 2, "max": 2})

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_superuser_force_override_runs_and_logs_event(self):
        self._calls(1)
        messages = self._post("core_order_start_full_production", self.order.pk, data={"force": "1"})

        self.assertTrue(any(m.startswith("Производство:") for m in messages), messages)
        self.assertEqual(self._full_jobs().filter(status=GenerationJob.Status.SUCCEEDED).count(), 1)
        (event,) = self._events(OrderEvent.BUDGET_OVERRIDE)
        self.assertEqual(event.payload["limit"], "day")
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload["reason"], "console confirmation")
        self.assertEqual(self._events(OrderEvent.BUDGET_BLOCKED), [])

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_staff_operator_cannot_override(self):
        self.client.force_login(self.operator)
        self._calls(1)
        messages = self._post("core_order_start_full_production", self.order.pk, data={"force": "1"})

        self.assertEqual(messages, ["Лимит вызовов в день исчерпан: 2/2. Генерация не запущена."])
        self.assertEqual(self._full_jobs().filter(status=GenerationJob.Status.SUCCEEDED).count(), 0)
        self.assertEqual(len(self._events(OrderEvent.BUDGET_BLOCKED)), 1)
        self.assertEqual(self._events(OrderEvent.BUDGET_OVERRIDE), [])

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_calls_from_yesterday_do_not_count_for_today(self):
        self._calls(5, when=timezone.now() - timezone.timedelta(days=1))
        messages = self._post("core_order_start_full_production", self.order.pk)
        self.assertTrue(messages[0].startswith("Производство:"), messages)

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_MONTH=3)
    def test_month_limit_blocks_preview(self):
        other = self._make_order(self.identity.user, self.order.product, self.order.style, status=Order.Status.PAID)
        self._calls(2, order=other)  # 2 + 2 preview jobs = 4 this month
        messages = self._post("core_order_generate_preview", other.pk)

        self.assertEqual(messages, ["Лимит вызовов в месяц исчерпан: 4/3. Генерация не запущена."])
        self.assertEqual(GenerationJob.objects.filter(order=other, task_type=GenerationJob.TaskType.PREVIEW).count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.status, Order.Status.PAID)

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=3)
    def test_order_limit_blocks_regeneration(self):
        self._calls(2)  # + preview = 3 calls on this order
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        messages = self._post("core_order_regenerate_slots", self.order.pk, data={"slot_keys": "e0"})

        self.assertEqual(messages, ["Лимит вызовов на заказ исчерпан: 3/3. Генерация не запущена."])
        self.assertEqual(self._full_jobs().count(), 2)

    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=2)
    def test_slot_limit_blocks_force_retry_of_that_slot_only(self):
        # force retry applies to ambiguous-failed slots; the guard runs inside
        # the service after that domain check
        self._calls(2, slot_key="e0", ambiguous=True)
        self._calls(1, slot_key="e1", ambiguous=True)
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])

        messages = self._post("core_order_force_retry_slot", self.order.pk, "e0")
        self.assertEqual(messages, ["Лимит попыток на слот (e0) исчерпан: 2/2. Генерация не запущена."])
        self.assertEqual(self._full_jobs().filter(slot_key="e0").count(), 2)

        # e1 has one attempt left: the gate lets the action through (the
        # service then decides on its own rules — here "not blocked").
        messages = self._post("core_order_force_retry_slot", self.order.pk, "e1")
        self.assertFalse(messages[0].startswith("Лимит"), messages)

    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=1)
    def test_slot_limit_on_start_checks_open_slots_only(self):
        # e0 already produced (FINAL asset) with 1 attempt: not checked again.
        job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.FULL, status=GenerationJob.Status.SUCCEEDED,
            attempt=1, provider="fake", slot_key="e0", started_at=timezone.now(),
        )
        GeneratedAsset.objects.create(order=self.order, job=job, kind=GeneratedAsset.Kind.FINAL, slot_key="e0",
                                      storage_key="generated/final-e0.png", size_bytes=1)
        decision = BudgetService().check(self.order, "full_start")
        self.assertEqual([l.slot_key for l in decision.limits if l.key == "slot"], ["e1", "e2"])
        self.assertIsNone(decision.blocked)

    def test_no_limits_means_current_behaviour(self):
        self._calls(20)
        messages = self._post("core_order_start_full_production", self.order.pk)
        self.assertTrue(messages[0].startswith("Производство:"), messages)
        self.assertEqual(self._events(OrderEvent.BUDGET_BLOCKED), [])

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=1)
    def test_confirmation_page_shows_budget_warning_and_override_control(self):
        response = self.client.get(reverse("admin:core_order_start_full_production", args=[self.order.pk]))
        self.assertContains(response, "Лимит вызовов в день исчерпан: 1/1")
        self.assertContains(response, 'name="force" value="1"')

        self.client.force_login(self.operator)
        response = self.client.get(reverse("admin:core_order_start_full_production", args=[self.order.pk]))
        self.assertContains(response, "Переопределить лимит может только суперпользователь")
        self.assertNotContains(response, 'name="force" value="1"')


@override_settings(**UNLIMITED, PILOT_IMAGE_CALL_COST_RUB=12.5)
class ConsoleCostFiguresTests(BudgetFixture):
    """DRF-2111: the console never prices history with today's tariff.
    Jobs created directly in these fixtures carry no cost snapshot → they
    are UNKNOWN (and "possibly billable"), whatever PILOT_IMAGE_CALL_COST_RUB
    says now. Priced jobs are covered by tests_generation_cost."""

    def test_order_card_shows_expenses_block_with_unknown_cost(self):
        self._calls(2, slot_key="e0")
        self._calls(1, task=GenerationJob.TaskType.REVISION)
        response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertContains(response, "Расходы")
        self.assertContains(response, "вызовов: 4 (превью 1 / правки 1 / производство 2)")
        self.assertContains(response, "токенов: 3000")
        self.assertContains(response, "AI всего: неизвестна")  # cost only in «Экономика заказа»
        self.assertNotContains(response, "стоимость: неизвестна")
        self.assertContains(response, "возможно платных: 4")
        self.assertNotContains(response, "50 ₽")
        self.assertContains(response, "попыток на слот: max 2 / без лимита")

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=10)
    def test_order_list_has_column_and_budget_header(self):
        response = self.client.get(reverse("admin:core_order_changelist"))
        self.assertContains(response, "Вызовы/₽")
        self.assertContains(response, "1 / неизвестна")
        self.assertContains(response, "Сегодня: 1 вызовов / лимит 10, стоимость неизвестна (возможно платных: 1)")
        self.assertContains(response, "Месяц: 1 вызовов / без лимита, стоимость неизвестна (возможно платных: 1)")
        self.assertNotContains(response, "12.5 ₽")

    def test_order_costs_service_reports_unknown_not_zero(self):
        costs = BudgetService().order_costs(self.order)
        self.assertEqual(costs["calls"], 1)
        self.assertEqual(costs["tokens"], 3000)
        self.assertNotIn("rub", costs)
        self.assertEqual(costs["cost"]["known_count"], 0)
        self.assertEqual(costs["cost"]["known_cost_minor"], 0)
        self.assertEqual(costs["cost"]["unknown_price_count"], 1)
        self.assertEqual(costs["cost"]["possibly_billable_count"], 1)


class RefundEventAndFilterTests(BudgetFixture):
    def test_refunded_event_is_counted_in_snapshot(self):
        self.assertEqual(PilotMetricsService().snapshot()["payments"]["refunded"], 0)
        OrderEvent.objects.create(order=self.order, event_type=OrderEvent.PAYMENT_REFUNDED,
                                  payload={"amount_minor": 460, "currency": "XTR"})
        OrderEvent.objects.create(order=self.order, event_type=OrderEvent.PAYMENT_REFUNDED)
        payments = PilotMetricsService().snapshot()["payments"]
        self.assertEqual(payments["refunded"], 1)  # per order
        self.assertEqual(payments["orders_paid"], 1)

    def test_event_admin_filter_offers_budget_and_refund_types(self):
        lookups = dict(EventTypeFilter(None, {}, OrderEvent, None).lookups(None, None))
        for value in (OrderEvent.BUDGET_BLOCKED, OrderEvent.BUDGET_OVERRIDE, OrderEvent.BUDGET_ALERT,
                      OrderEvent.PAYMENT_REFUNDED, OrderEvent.Type.STATUS_CHANGED):
            self.assertIn(value, lookups)
        response = self.client.get(reverse("admin:core_orderevent_changelist") + f"?event_type={OrderEvent.BUDGET_BLOCKED}")
        self.assertEqual(response.status_code, 200)
