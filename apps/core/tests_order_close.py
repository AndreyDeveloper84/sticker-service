"""«Закрыть заказ» (DRF-2167 follow-up): transitions, OrderCloseService and
the console action — superuser only, mandatory reason, money note, refused
while an attempt is queued / running, CANCELLED vs FAILED, nothing deleted."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderEvent,
    Payment,
    Product,
    Style,
    User,
)
from apps.core.services.generation_cost import COST_KEY
from apps.core.services.order_close import ORDER_CLOSED, OrderCloseError, OrderCloseService
from apps.core.services.order_state import OrderStateService


class CloseFixture(TestCase):
    def setUp(self):
        user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-1"
        )
        self.product = Product.objects.create(code="single", name="Один стикер", config={})
        self.style = Style.objects.create(code="3d", name="3D", config={})
        self.user = user

    def order(self, status=Order.Status.PAID, *, paid=True):
        order = Order.objects.create(
            user=self.user, channel_identity=self.identity, product=self.product, style=self.style, status=status
        )
        if paid:
            Payment.objects.create(
                order=order, provider="yookassa", status=Payment.Status.CONFIRMED, amount_minor=10000,
                currency="RUB", external_payment_id=f"p-{order.pk}", confirmed_at=timezone.now(),
            )
        return order

    def job(self, order, status, *, started=True, billable=None, age=timedelta(minutes=1)):
        cost = {"cost_source": "CONFIG_SNAPSHOT", "cost_minor": 700, "billable": billable}
        job = GenerationJob.objects.create(
            order=order, task_type=GenerationJob.TaskType.PREVIEW, status=status, attempt=1, provider="fake",
            input_metadata={COST_KEY: cost} if billable is not None else {},
        )
        when = timezone.now() - age
        GenerationJob.objects.filter(pk=job.pk).update(created_at=when, started_at=when if started else None)
        job.refresh_from_db()
        return job


class TransitionTests(CloseFixture):
    def test_cancelled_reachable_after_payment_only_from_the_listed_statuses(self):
        for status in (
            Order.Status.PAID, Order.Status.PREVIEW_GENERATING, Order.Status.INTERNAL_PREVIEW_REVIEW,
            Order.Status.PREVIEW_REVIEW, Order.Status.QUALITY_CONTROL,
        ):
            self.assertIn(Order.Status.CANCELLED, OrderStateService.allowed_targets(status), status)
        for status in (
            Order.Status.REVISION_REQUESTED, Order.Status.REVISION_GENERATING, Order.Status.PACK_GENERATING,
            Order.Status.READY_FOR_DELIVERY, Order.Status.DELIVERY_IN_PROGRESS,
        ):
            self.assertNotIn(Order.Status.CANCELLED, OrderStateService.allowed_targets(status), status)
            self.assertIn(Order.Status.FAILED, OrderStateService.allowed_targets(status), status)
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.CANCELLED), set())


class ServiceTests(CloseFixture):
    def test_reason_and_payment_note_are_mandatory(self):
        order = self.order()
        with self.assertRaises(OrderCloseError):
            OrderCloseService.close(order=order, actor_ref="op", reason="  ", payment_note="not_required")
        with self.assertRaises(OrderCloseError):
            OrderCloseService.close(order=order, actor_ref="op", reason="legacy", payment_note="whatever")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertFalse(OrderEvent.objects.filter(order=order, event_type=ORDER_CLOSED).exists())

    def test_paid_without_generation_becomes_cancelled_with_event(self):
        order = self.order()
        closed = OrderCloseService.close(order=order, actor_ref="owner", reason="legacy test order", payment_note="not_required")
        self.assertEqual(closed.status, Order.Status.CANCELLED)
        event = OrderEvent.objects.get(order=order, event_type=ORDER_CLOSED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "owner")
        self.assertEqual((event.from_status, event.to_status), (Order.Status.PAID, Order.Status.CANCELLED))
        self.assertEqual(event.payload["reason"], "legacy test order")
        self.assertEqual(event.payload["payment_note_text"], "возврат не требуется")
        self.assertFalse(event.payload["had_billable_generation"])
        self.assertEqual(event.payload["payments"][0]["amount_minor"], 10000)
        # the status change went through the state service (funnel event)
        self.assertTrue(
            OrderEvent.objects.filter(
                order=order, event_type=OrderEvent.Type.STATUS_CHANGED, to_status=Order.Status.CANCELLED
            ).exists()
        )
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)  # nothing deleted

    def test_billable_generation_makes_the_close_failed(self):
        order = self.order(Order.Status.INTERNAL_PREVIEW_REVIEW)
        job = self.job(order, GenerationJob.Status.SUCCEEDED, billable=True)
        GeneratedAsset.objects.create(
            order=order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key="p", mime_type="image/png", size_bytes=1
        )
        closed = OrderCloseService.close(order=order, actor_ref="owner", reason="likeness lost", payment_note="needed")
        self.assertEqual(closed.status, Order.Status.FAILED)
        event = OrderEvent.objects.get(order=order, event_type=ORDER_CLOSED)
        self.assertTrue(event.payload["had_billable_generation"])
        self.assertEqual(event.payload["jobs"], [{"id": job.pk, "task_type": "preview", "status": "succeeded"}])
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)
        self.assertEqual(GeneratedAsset.objects.filter(order=order).count(), 1)

    def test_not_billable_or_legacy_attempts(self):
        # a failed attempt that provably never reached the provider → CANCELLED
        clean = self.order(Order.Status.PREVIEW_GENERATING)
        self.job(clean, GenerationJob.Status.FAILED, billable=False)
        self.assertEqual(
            OrderCloseService.close(order=clean, actor_ref="o", reason="x", payment_note="not_required").status,
            Order.Status.CANCELLED,
        )
        # a legacy attempt without a cost snapshot → fail closed → FAILED
        legacy = self.order(Order.Status.QUALITY_CONTROL)
        self.job(legacy, GenerationJob.Status.SUCCEEDED, billable=None)
        self.assertEqual(
            OrderCloseService.close(order=legacy, actor_ref="o", reason="x", payment_note="refunded").status,
            Order.Status.FAILED,
        )

    def test_status_without_cancelled_transition_closes_as_failed(self):
        order = self.order(Order.Status.PACK_GENERATING)
        self.job(order, GenerationJob.Status.FAILED, billable=False)  # nothing billable, but no CANCELLED exit
        closed = OrderCloseService.close(order=order, actor_ref="o", reason="x", payment_note="needed")
        self.assertEqual(closed.status, Order.Status.FAILED)

    def test_refused_while_an_attempt_is_queued_or_running(self):
        for status in (GenerationJob.Status.PENDING, GenerationJob.Status.RUNNING):
            order = self.order(Order.Status.PREVIEW_GENERATING)
            self.job(order, status, started=status == GenerationJob.Status.RUNNING)
            with self.assertRaises(OrderCloseError) as ctx:
                OrderCloseService.close(order=order, actor_ref="o", reason="x", payment_note="needed")
            self.assertIn("Снять из очереди", str(ctx.exception))
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.PREVIEW_GENERATING)

    def test_stale_running_is_reaped_then_close_is_failed(self):
        order = self.order(Order.Status.PREVIEW_GENERATING)
        job = self.job(order, GenerationJob.Status.RUNNING, billable=True, age=timedelta(minutes=16))
        closed = OrderCloseService.close(order=order, actor_ref="o", reason="worker died", payment_note="needed")
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJob.Status.FAILED)
        self.assertEqual(job.output_metadata["failure_class"], "ambiguous")
        self.assertEqual(closed.status, Order.Status.FAILED)  # possibly billable → never CANCELLED

    def test_terminal_and_pre_payment_orders_cannot_be_closed(self):
        for status in (Order.Status.AWAITING_PHOTOS, Order.Status.DELIVERED, Order.Status.CANCELLED, Order.Status.FAILED):
            order = self.order(status, paid=False)
            self.assertFalse(OrderCloseService.closable(order))
            with self.assertRaises(OrderCloseError):
                OrderCloseService.close(order=order, actor_ref="o", reason="x", payment_note="not_required")


class ConsoleTests(CloseFixture):
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser(username="owner", email="o@example.com", password="pass")
        self.staff = get_user_model().objects.create_user(username="staff", password="pass", is_staff=True)
        from django.contrib.auth.models import Permission

        self.staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="core"))

    def url(self, order):
        return reverse("admin:core_order_close", args=[order.pk])

    def change(self, order):
        return reverse("admin:core_order_change", args=[order.pk])

    def messages(self, response):
        import re

        return [re.sub(r"<[^>]+>", "", m) for m in re.findall(r'<li class="(?:error|success)">(.*?)</li>', response.content.decode(), re.S)]

    def test_link_in_secondary_actions_only_for_closable_orders_without_active_jobs(self):
        self.client.force_login(self.superuser)
        order = self.order()
        content = self.client.get(self.change(order)).content.decode()
        self.assertIn(self.url(order), content)
        self.assertIn("Закрыть заказ…", content)
        self.job(order, GenerationJob.Status.PENDING, started=False)
        content = self.client.get(self.change(order)).content.decode()
        self.assertNotIn(self.url(order), content)  # «Дополнительно» is empty while generating
        done = self.order(Order.Status.DELIVERED)
        self.assertNotIn(self.url(done), self.client.get(self.change(done)).content.decode())

    def test_staff_user_is_refused(self):
        self.client.force_login(self.staff)
        order = self.order()
        response = self.client.post(self.url(order), {"reason": "x", "payment_note": "not_required"}, follow=True)
        self.assertIn("только суперпользователь", " ".join(self.messages(response)))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_confirmation_form_and_missing_reason(self):
        self.client.force_login(self.superuser)
        order = self.order()
        content = self.client.get(self.url(order)).content.decode()
        self.assertIn(f"Закрыть заказ #{order.pk}", content)
        self.assertIn("«Отменён»", content)
        self.assertIn('name="reason"', content)
        self.assertIn('name="payment_note"', content)
        self.assertIn("возврат не требуется", content)
        self.assertIn("#" + str(order.payments.get().pk) + " yookassa confirmed 10000 RUB", content)
        response = self.client.post(self.url(order), {"reason": "   ", "payment_note": "not_required"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Укажите причину", response.content.decode())
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_close_from_console(self):
        self.client.force_login(self.superuser)
        order = self.order(Order.Status.PREVIEW_REVIEW)
        self.job(order, GenerationJob.Status.SUCCEEDED, billable=True)
        content = self.client.get(self.url(order)).content.decode()
        self.assertIn("«Ошибка»", content)  # a billable attempt exists
        response = self.client.post(self.url(order), {"reason": "legacy staging order", "payment_note": "refunded"}, follow=True)
        self.assertIn(f"Заказ #{order.pk} закрыт: «", " ".join(self.messages(response)))
        self.assertIn("возврат сделан", " ".join(self.messages(response)))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FAILED)
        event = OrderEvent.objects.get(order=order, event_type=ORDER_CLOSED)
        self.assertEqual(event.actor_ref, "owner")
        # the card now says «Заказ завершён с ошибкой», no actions
        content = self.client.get(self.change(order)).content.decode()
        self.assertNotIn(self.url(order), content)

    def test_refused_while_generating(self):
        self.client.force_login(self.superuser)
        order = self.order(Order.Status.PREVIEW_GENERATING)
        self.job(order, GenerationJob.Status.PENDING, started=False)
        response = self.client.post(self.url(order), {"reason": "x", "payment_note": "needed"}, follow=True)
        self.assertIn("действия пока недоступны", " ".join(self.messages(response)))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_GENERATING)
