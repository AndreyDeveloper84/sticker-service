"""DRF-2167: Operator Attention Queue — one bucket per live order state, a
fixed number of queries, and the console page with links to the orders."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import (
    ChannelIdentity,
    FinalDelivery,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderEvent,
    Product,
    Revision,
    Style,
    User,
)
from apps.core.services.attention import PAID_IDLE_AFTER, AttentionQueue, age_text
from apps.core.services.generation_queue import QUEUE_LOST


class AttentionFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=self.user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-1", display_name="Аня"
        )
        self.product = Product.objects.create(code="single", name="Один стикер", config={})
        self.style = Style.objects.create(code="3d", name="3D", config={})
        self.now = timezone.now()

    def order(self, status, *, age=timedelta(minutes=1), **fields):
        order = Order.objects.create(
            user=self.user, channel_identity=self.identity, product=self.product, style=self.style,
            status=status, **fields,
        )
        OrderEvent.objects.create(
            order=order, event_type=OrderEvent.Type.STATUS_CHANGED, from_status="", to_status=status,
        )
        OrderEvent.objects.filter(order=order).update(created_at=self.now - age)
        return order

    def job(self, order, status, *, task=GenerationJob.TaskType.PREVIEW, slot="", age=timedelta(minutes=1),
            output=None, error="", attempt=1):
        job = GenerationJob.objects.create(
            order=order, task_type=task, slot_key=slot, status=status, attempt=attempt, provider="fake",
            output_metadata=output or {}, error=error,
        )
        when = self.now - age
        fields = {"created_at": when}
        if status in (GenerationJob.Status.RUNNING, GenerationJob.Status.SUCCEEDED, GenerationJob.Status.FAILED):
            fields["started_at"] = when
        if status in (GenerationJob.Status.SUCCEEDED, GenerationJob.Status.FAILED):
            fields["finished_at"] = when
        GenerationJob.objects.filter(pk=job.pk).update(**fields)
        job.refresh_from_db()
        return job

    def classify(self, order):
        buckets = AttentionQueue(now=self.now).build()
        for key, items in buckets.items():
            for item in items:
                if item.order.pk == order.pk:
                    return key, item
        return None, None


class BucketTests(AttentionFixture):
    def test_pre_payment_and_terminal_orders_are_not_listed(self):
        for status in (
            Order.Status.DRAFT, Order.Status.AWAITING_PHOTOS, Order.Status.READY_FOR_CHECKOUT,
            Order.Status.AWAITING_PAYMENT, Order.Status.DELIVERED, Order.Status.CANCELLED, Order.Status.FAILED,
        ):
            with self.subTest(status=status):
                order = self.order(status, age=timedelta(days=3))
                self.assertEqual(self.classify(order), (None, None))

    def test_just_paid_is_quiet_then_paid_idle(self):
        fresh = self.order(Order.Status.PAID, age=timedelta(minutes=2))
        self.assertEqual(self.classify(fresh), (None, None))
        idle = self.order(Order.Status.PAID, age=PAID_IDLE_AFTER + timedelta(minutes=1))
        key, item = self.classify(idle)
        self.assertEqual(key, "paid_idle")
        self.assertEqual(age_text(item.age), "16 мин")
        self.assertIn("Сгенерировать превью", item.next_step)

    def test_queued_job(self):
        order = self.order(Order.Status.PREVIEW_GENERATING)
        job = self.job(order, GenerationJob.Status.PENDING, age=timedelta(minutes=2))
        key, item = self.classify(order)
        self.assertEqual(key, "queued")
        self.assertIn(f"job #{job.pk} (превью)", item.detail)
        self.assertEqual(age_text(item.age), "2 мин")

    def test_generating_job(self):
        order = self.order(Order.Status.PACK_GENERATING)
        job = self.job(order, GenerationJob.Status.RUNNING, task=GenerationJob.TaskType.FULL, slot="hello",
                       age=timedelta(seconds=42))
        key, item = self.classify(order)
        self.assertEqual(key, "generating")
        self.assertIn(f"job #{job.pk} (производство «hello»)", item.detail)
        self.assertEqual(age_text(item.age), "42 с")

    def test_pending_waiting_for_worker_needs_recovery(self):
        order = self.order(Order.Status.PREVIEW_GENERATING)
        job = self.job(order, GenerationJob.Status.PENDING, age=timedelta(minutes=6))
        key, item = self.classify(order)
        self.assertEqual(key, "recovery_required")
        self.assertIn("Снять из очереди", item.next_step)
        self.assertIn(f"job #{job.pk}", item.detail)

    def test_stale_running_needs_recovery(self):
        order = self.order(Order.Status.REVISION_GENERATING)
        self.job(order, GenerationJob.Status.RUNNING, task=GenerationJob.TaskType.REVISION, age=timedelta(minutes=16))
        key, item = self.classify(order)
        self.assertEqual(key, "recovery_required")
        self.assertIn("висит в RUNNING", item.detail)

    def test_ambiguous_and_queue_lost_need_recovery(self):
        blocked = self.order(Order.Status.PACK_GENERATING)
        self.job(blocked, GenerationJob.Status.FAILED, task=GenerationJob.TaskType.FULL, slot="bye",
                 output={"failure_class": "ambiguous"})
        key, item = self.classify(blocked)
        self.assertEqual(key, "recovery_required")
        self.assertIn("Принудительный повтор", item.next_step)
        self.assertIn("возможно, оплачено", item.detail)

        lost = self.order(Order.Status.PREVIEW_GENERATING)
        self.job(lost, GenerationJob.Status.FAILED, output={"failure_class": QUEUE_LOST})
        key, item = self.classify(lost)
        self.assertEqual(key, "recovery_required")
        self.assertIn("снята из очереди", item.detail)
        self.assertIn("заново", item.next_step)

    def test_failed_attempt_is_an_error(self):
        order = self.order(Order.Status.PREVIEW_GENERATING)
        job = self.job(order, GenerationJob.Status.FAILED, output={"failure_class": "api"}, error="provider unavailable")
        key, item = self.classify(order)
        self.assertEqual(key, "error")
        self.assertIn(f"job #{job.pk} (превью) — ошибка: provider unavailable", item.detail)

    def test_failed_then_new_attempt_is_not_an_error(self):
        order = self.order(Order.Status.PREVIEW_GENERATING)
        self.job(order, GenerationJob.Status.FAILED, output={"failure_class": "api"}, age=timedelta(minutes=5))
        self.job(order, GenerationJob.Status.PENDING, attempt=2)
        key, _item = self.classify(order)
        self.assertEqual(key, "queued")

    def test_operator_action_states(self):
        review = self.order(Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.job(review, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(self.classify(review)[0], "operator_action")
        self.assertIn("одобрите", self.classify(review)[1].next_step)

        pack = self.order(Order.Status.PACK_GENERATING)
        self.job(pack, GenerationJob.Status.SUCCEEDED, task=GenerationJob.TaskType.FULL, slot="hello")
        self.assertEqual(self.classify(pack)[0], "operator_action")
        self.assertIn("Запустить производство", self.classify(pack)[1].next_step)

        ready = self.order(Order.Status.READY_FOR_DELIVERY)
        self.assertEqual(self.classify(ready)[0], "operator_action")
        self.assertIn("Отправить набор", self.classify(ready)[1].next_step)

    def test_revision_requested(self):
        order = self.order(Order.Status.REVISION_REQUESTED, age=timedelta(hours=2, minutes=30))
        job = self.job(order, GenerationJob.Status.SUCCEEDED)
        asset = GeneratedAsset.objects.create(
            order=order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key="p", mime_type="image/png", size_bytes=1,
        )
        Revision.objects.create(order=order, source_preview=asset, category=Revision.Category.OTHER, customer_text="")
        key, item = self.classify(order)
        self.assertEqual(key, "revision_requested")
        self.assertEqual(item.detail, "категория: other")
        self.assertEqual(age_text(item.age), "2 ч 30 мин")
        self.assertIn("Сгенерировать правку", item.next_step)

    def test_waiting_customer_and_qc(self):
        waiting = self.order(Order.Status.PREVIEW_REVIEW, age=timedelta(days=3))
        key, item = self.classify(waiting)
        self.assertEqual(key, "waiting_customer")
        self.assertEqual(age_text(item.age), "3 дн")
        qc = self.order(Order.Status.QUALITY_CONTROL)
        self.assertEqual(self.classify(qc)[0], "waiting_qc")

    def test_partial_delivery(self):
        order = self.order(Order.Status.DELIVERY_IN_PROGRESS)
        FinalDelivery.objects.create(
            order=order, channel=ChannelIdentity.Channel.MAX, attempt=1, status=FinalDelivery.Status.FAILED,
            results=[
                {"slot_key": "hello", "status": "sent", "message_id": "m1"},
                {"slot_key": "bye", "status": "failed", "failure_class": "retryable"},
            ],
            summary={},
        )
        key, item = self.classify(order)
        self.assertEqual(key, "partial_delivery")
        self.assertIn("отправлено слотов: 1", item.detail)
        self.assertIn("не ушли: bye (retryable)", item.detail)
        self.assertIn("Продолжить доставку", item.next_step)

    def test_fixed_number_of_queries(self):
        for status in (
            Order.Status.PAID, Order.Status.PREVIEW_GENERATING, Order.Status.INTERNAL_PREVIEW_REVIEW,
            Order.Status.PREVIEW_REVIEW, Order.Status.PACK_GENERATING, Order.Status.QUALITY_CONTROL,
            Order.Status.DELIVERY_IN_PROGRESS,
        ):
            order = self.order(status, age=timedelta(hours=1))
            self.job(order, GenerationJob.Status.SUCCEEDED)
            self.job(order, GenerationJob.Status.FAILED, attempt=2, output={"failure_class": "api"})
        with self.assertNumQueries(4):
            buckets = AttentionQueue(now=self.now).build()
        self.assertEqual(sum(len(items) for items in buckets.values()), 7)


class AttentionPageTests(AttentionFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(
            get_user_model().objects.create_superuser(username="op", email="op@example.com", password="pass")
        )

    def test_page_lists_orders_with_links_and_next_steps(self):
        idle = self.order(Order.Status.PAID, age=timedelta(hours=1))
        waiting = self.order(Order.Status.PREVIEW_REVIEW, age=timedelta(minutes=30))
        response = self.client.get(reverse("admin:core_order_attention"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Требует внимания", content)
        self.assertIn("Оплачен, генерация не запущена (1)", content)
        self.assertIn("Ждём клиента (1)", content)
        for order in (idle, waiting):
            self.assertIn(reverse("admin:core_order_change", args=[order.pk]), content)
        self.assertIn("Нажмите «Сгенерировать превью».", content)
        self.assertIn("MAX · Один стикер", content)
        self.assertIn("1 ч 0 мин", content)

    def test_changelist_links_to_the_page(self):
        content = self.client.get(reverse("admin:core_order_changelist")).content.decode()
        self.assertIn(reverse("admin:core_order_attention"), content)
        self.assertIn("Требует внимания", content)
