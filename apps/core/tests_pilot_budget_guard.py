"""Pilot Budget Guard (DRF-2086, owner document): enforcement at the service
boundary. Numbering follows the owner's test list.

The fake provider counts calls: "0 provider calls" is asserted literally.
"""

import threading
from io import BytesIO
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import GenerationJob, Order, OrderEvent
from apps.core.services.budget import BudgetConfigError, BudgetExceeded, BudgetOverride
from apps.core.services.full_production import FullProductionService
from apps.core.services.generation import GenerationService
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.tests_pilot_budget import UNLIMITED, BudgetFixture
from apps.core.tests_qc import make_image


class CountingProvider:
    name = "fake"

    def __init__(self, *, barrier=None):
        self.calls = 0
        self.lock = threading.Lock()
        self.barrier = barrier

    def generate_preview(self, request):
        with self.lock:
            self.calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


@override_settings(**UNLIMITED)
class BudgetGuardServiceTests(BudgetFixture):
    def setUp(self):
        super().setUp()
        self.provider = CountingProvider()
        self.generation = GenerationService(provider=self.provider, storage=self.storage)
        self.production = FullProductionService(provider=self.provider, storage=self.storage)

    def _paid_order(self):
        return self._make_order(self.identity.user, self.order.product, self.order.style, status=Order.Status.PAID)

    # (1) under limit
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=5)
    def test_01_under_limit_preview_calls_provider_exactly_once(self):
        order = self._paid_order()
        self.generation.generate_preview(order=order)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.PREVIEW).count(), 2)

    # (2) order limit
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=1)
    def test_02_order_limit_zero_provider_calls(self):
        order = self._paid_order()  # fixture preview job = 1 call on this order
        with self.assertRaisesMessage(BudgetExceeded, "Лимит вызовов на заказ исчерпан: 1/1"):
            self.generation.generate_preview(order=order)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID, "the whole transaction rolled back")

    # (3) daily limit
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_03_daily_limit_zero_provider_calls(self):
        order = self._paid_order()  # 2 preview jobs today (fixture orders)
        with self.assertRaisesMessage(BudgetExceeded, "вызовов в день"):
            self.generation.generate_preview(order=order)
        self.assertEqual(self.provider.calls, 0)

    # (4) monthly limit
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_MONTH=1)
    def test_04_monthly_limit_zero_provider_calls(self):
        # fixture preview job = 1 call this month
        with self.assertRaisesMessage(BudgetExceeded, "вызовов в месяц"):
            self.production.start(order=self.order)
        self.assertEqual(self.provider.calls, 0)
        self.assertFalse(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.FULL).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW, "no PACK_GENERATING without a job")

    # (5) slot attempts below limit
    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=3)
    def test_05_slot_attempts_below_limit_allowed(self):
        self._calls(2, slot_key="e0")  # two failed attempts, one left
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        plan = self.production.retry_failed(order=self.order)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual([slot.slot_key for slot in plan if slot.status == "succeeded"], ["e0"])

    # (6) slot attempts at limit
    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=2)
    def test_06_slot_attempts_at_limit_zero_provider_calls(self):
        self._calls(2, slot_key="e0")
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        with self.assertRaisesMessage(BudgetExceeded, "Лимит попыток на слот (e0) исчерпан: 2/2"):
            self.production.retry_failed(order=self.order)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(GenerationJob.objects.filter(slot_key="e0").count(), 2)

    # (7) one slot's limit leaves the others untouched
    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=2)
    def test_07_one_slot_limit_does_not_block_other_slots(self):
        self._calls(2, slot_key="e0")
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        with self.assertRaises(BudgetExceeded):
            self.production.regenerate_slots(order=self.order, slot_keys=["e0"])
        self.assertEqual(self.provider.calls, 0)
        # e1 has no attempts: it may still be (re)generated
        plan = self.production.regenerate_slots(order=self.order, slot_keys=["e1"])
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual([slot.slot_key for slot in plan if slot.status == "succeeded"], ["e1"])

    # (8) regenerate goes through the same guard
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=1)
    def test_08_regenerate_uses_the_same_guard(self):
        self.order.status = Order.Status.PACK_GENERATING
        self.order.save(update_fields=["status"])
        with self.assertRaisesMessage(BudgetExceeded, "вызовов на заказ"):
            self.production.regenerate_slots(order=self.order, slot_keys=["e0"])
        self.assertEqual(self.provider.calls, 0)

    # (9) QC retry is not a bypass
    @override_settings(PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=1)
    def test_09_qc_retry_path_is_guarded(self):
        plan = self.production.start(order=self.order, max_slots=None)
        self.assertEqual([slot.status for slot in plan], ["succeeded"] * 3)
        self.assertEqual(self.provider.calls, 3)
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=self.order)
        checklist = {criterion: True for criterion in HUMAN_CRITERIA}
        checklist[next(iter(HUMAN_CRITERIA))] = False
        qc.finalize_report(report=report, checklist=checklist)
        report.refresh_from_db()
        qc.request_retry(report=report, slot_keys=["e0"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PACK_GENERATING)
        # QC-retry slots go through regenerate_slots → the same guard
        with self.assertRaisesMessage(BudgetExceeded, "попыток на слот (e0)"):
            self.production.regenerate_slots(order=self.order, slot_keys=["e0"])
        self.assertEqual(self.provider.calls, 3)

    # (10) staff cannot override — through the console
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_10_staff_cannot_override(self):
        self._calls(1, task=GenerationJob.TaskType.REVISION)  # + fixture preview = 2 of 2 today
        self.client.force_login(self.operator)
        messages = self._post("core_order_start_full_production", self.order.pk, data={"force": "1"})
        self.assertTrue(messages[0].startswith("Лимит вызовов в день исчерпан"), messages)
        self.assertFalse(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.FULL).exists())
        self.assertEqual(self._events(OrderEvent.BUDGET_OVERRIDE), [])
        self.assertEqual(len(self._events(OrderEvent.BUDGET_BLOCKED)), 1)

    # (11) superuser without explicit confirmation is blocked too
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_11_superuser_without_confirmation_is_blocked(self):
        self._calls(1, task=GenerationJob.TaskType.REVISION)  # + fixture preview = 2 of 2 today
        messages = self._post("core_order_start_full_production", self.order.pk)  # no force=1
        self.assertTrue(messages[0].startswith("Лимит вызовов в день исчерпан"), messages)
        self.assertFalse(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.FULL).exists())
        # and directly on the service: no override object → blocked
        with self.assertRaises(BudgetExceeded):
            self.production.start(order=self.order)
        self.assertEqual(self.provider.calls, 0)

    # (12) explicit override → exactly one provider call
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_12_explicit_override_calls_provider_once(self):
        self._calls(1, task=GenerationJob.TaskType.REVISION)  # + fixture preview = 2 of 2 today
        plan = self.production.start(order=self.order, budget_override=BudgetOverride(actor_ref="root", reason="test"))
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(plan[0].status, "succeeded")

    # (13) override leaves an audit trail, in the same transaction as the job
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=2)
    def test_13_override_writes_audit_event(self):
        self._calls(1, task=GenerationJob.TaskType.REVISION)  # + fixture preview = 2 of 2 today
        self.production.start(order=self.order, budget_override=BudgetOverride(actor_ref="root", reason="live test"))
        (event,) = self._events(OrderEvent.BUDGET_OVERRIDE)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload, {"action": "full_start", "limit": "day", "slot_key": "", "used": 2, "max": 2,
                                         "reason": "live test"})
        job = GenerationJob.objects.get(task_type=GenerationJob.TaskType.FULL, status=GenerationJob.Status.SUCCEEDED)
        self.assertLessEqual(event.created_at, job.finished_at)

    # (14) invalid env → fail closed before the provider
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY="ten")
    def test_14_invalid_config_fails_closed(self):
        order = self._paid_order()
        with self.assertRaisesMessage(BudgetConfigError, "PILOT_MAX_IMAGE_CALLS_PER_DAY"):
            self.generation.generate_preview(order=order)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)
        # console: Russian message, no job, no override possible
        messages = self._post("core_order_generate_preview", order.pk, data={"force": "1"})
        self.assertIn("Некорректная настройка лимита PILOT_MAX_IMAGE_CALLS_PER_DAY", messages[0])
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)

    # (15) unset / 0 semantics
    def test_15_unset_and_zero_semantics(self):
        from apps.core.services.budget import limit_value

        with override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=None, PILOT_MAX_IMAGE_CALLS_PER_ORDER=None,
                               PILOT_MAX_FULL_ATTEMPTS_PER_SLOT=None, PILOT_MAX_IMAGE_CALLS_PER_MONTH=None), \
                patch.dict("os.environ", {}, clear=False):
            for name in ("PILOT_MAX_IMAGE_CALLS_PER_DAY", "PILOT_MAX_IMAGE_CALLS_PER_MONTH",
                         "PILOT_MAX_IMAGE_CALLS_PER_ORDER", "PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"):
                import os
                os.environ.pop(name, None)
            self.assertIsNone(limit_value("PILOT_MAX_IMAGE_CALLS_PER_DAY"))
            self.assertIsNone(limit_value("PILOT_MAX_IMAGE_CALLS_PER_MONTH"))
            self.assertEqual(limit_value("PILOT_MAX_IMAGE_CALLS_PER_ORDER"), 15)
            self.assertEqual(limit_value("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"), 3)
        with override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=0, PILOT_MAX_FULL_ATTEMPTS_PER_SLOT="0"):
            self.assertIsNone(limit_value("PILOT_MAX_IMAGE_CALLS_PER_ORDER"))
            self.assertIsNone(limit_value("PILOT_MAX_FULL_ATTEMPTS_PER_SLOT"))
        # behaviour with everything unlimited: 20 prior calls do not block
        self._calls(20)
        self.production.start(order=self.order)
        self.assertEqual(self.provider.calls, 1)

    # cumulative check inside one multi-slot call
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=2)
    def test_multi_slot_run_is_checked_cumulatively(self):
        # fixture preview = 1 call; limit 2 → the first slot fits, the second
        # does not → the WHOLE call rolls back: no job, no provider call.
        with self.assertRaisesMessage(BudgetExceeded, "вызовов на заказ исчерпан: 2/2"):
            self.production.start(order=self.order, max_slots=None)
        self.assertEqual(self.provider.calls, 0)
        self.assertFalse(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.FULL).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PREVIEW_REVIEW)

    # regenerate preview keeps the order on internal review when blocked
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=1)
    def test_regenerate_preview_blocked_keeps_internal_review(self):
        order = self._paid_order()
        order.status = Order.Status.INTERNAL_PREVIEW_REVIEW
        order.save(update_fields=["status"])
        messages = self._post("core_order_regenerate_preview", order.pk)
        self.assertTrue(messages[0].startswith("Лимит вызовов на заказ исчерпан"), messages)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)


@override_settings(**{**UNLIMITED, "PILOT_MAX_IMAGE_CALLS_PER_DAY": 2})
class BudgetGuardRaceTests(TransactionTestCase):
    """(16) Two concurrent starts on the last free daily unit → exactly one
    provider call. Each thread has its own DB connection; on PostgreSQL the
    advisory lock serializes the counters, so the second transaction sees
    the first job and is refused."""

    def setUp(self):
        from apps.core.tests_pilot_budget import EMOTIONS, PACK3
        from apps.core.models import ChannelIdentity, OrderPhoto, Product, Style, User
        import tempfile
        from pathlib import Path
        from apps.core.storage import LocalMediaStorage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="race")
        product = Product.objects.create(code="pack3", name="Pack 3", config=PACK3)
        style = Style.objects.create(code="comic", name="Comic")
        self.orders = []
        for index in range(2):
            order = Order.objects.create(user=user, channel_identity=identity, product=product, style=style,
                                         status=Order.Status.PAID, selection={"emotions": [e["code"] for e in EMOTIONS]})
            key = f"orders/{order.pk}/source/photo.jpg"
            self.storage.save(key, BytesIO(b"photo"))
            OrderPhoto.objects.create(order=order, storage_key=key, original_filename="photo.jpg", mime_type="image/jpeg", size_bytes=5)
            self.orders.append(order)
        # one call already used today → exactly one unit left for two racers
        GenerationJob.objects.create(order=self.orders[0], task_type=GenerationJob.TaskType.REVISION,
                                     status=GenerationJob.Status.FAILED, attempt=1, provider="fake",
                                     started_at=timezone.now())

    def test_16_race_on_last_unit_calls_provider_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("advisory lock serialization is PostgreSQL-only")
        provider = CountingProvider()
        outcomes = {}
        start = threading.Barrier(2, timeout=10)

        def worker(index):
            try:
                start.wait()
                GenerationService(provider=provider, storage=self.storage).generate_preview(order=self.orders[index])
                outcomes[index] = "ok"
            except BudgetExceeded:
                outcomes[index] = "blocked"
            except Exception as exc:  # noqa: BLE001
                outcomes[index] = f"error:{exc.__class__.__name__}"
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(outcomes.values()), ["blocked", "ok"], outcomes)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(GenerationJob.objects.filter(task_type=GenerationJob.TaskType.PREVIEW).count(), 1)
