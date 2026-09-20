"""DRF-2170 Customer Media Lifecycle.

Retention is OFF by default and the periods are the owner's env decision;
only media of terminal orders older than a period are planned; dry-run
deletes nothing; apply without the flag is refused; a customer request
purges a terminal order right away and clears the contact; the accounting
trail (Payment, GenerationJob + cost snapshot, OrderEvent, QcReport) stays;
``media.purged`` carries counts and bytes only; the console action is
superuser-only. Access: the admin file views need staff (tests_media,
tests here for the anonymous case); the metrics export has no PII
(tests_pilot_analytics.test_export_csv_columns_unknown_empty_no_pii).
"""

from datetime import timedelta
from io import BytesIO, StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import GeneratedAsset, GenerationJob, Order, OrderEvent, OrderPhoto, Payment, QcReport
from apps.core.services import generation_cost as gc
from apps.core.services.media_lifecycle import (
    KIND_FINAL,
    KIND_PREVIEW,
    KIND_SOURCE,
    MediaLifecycleError,
    MediaLifecycleService,
    is_purged,
    retention_policy,
)
from apps.core.tests_final_delivery import FinalDeliveryTestCase

OFF = dict(MEDIA_RETENTION_ENABLED=None, MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=None,
           MEDIA_RETENTION_PREVIEWS_DAYS=None, MEDIA_RETENTION_FINALS_DAYS=None)
ENV_CLEAR = {name: "" for name in OFF}


def env_off():
    return mock.patch.dict("os.environ", ENV_CLEAR)


def cfg(**overrides):
    """override_settings with the retention policy OFF unless overridden."""
    return override_settings(**{**OFF, **overrides})


class LifecycleFixture(TestCase):
    """A delivered order with a source photo, a preview, a normalized final
    (+ provider original), a confirmed payment, a priced job and a QC
    report — every object class the lifecycle must treat correctly.
    (FinalDeliveryTestCase helpers without inheriting its tests.)"""

    base_setup = FinalDeliveryTestCase.setUp
    make_order = FinalDeliveryTestCase.make_order
    add_final = FinalDeliveryTestCase.add_final

    def setUp(self):
        self.base_setup()
        self.order = self.make_order(quantity=1, status=Order.Status.DELIVERED)
        self.order.selection = {**self.order.selection, "contact": "Анна, @anna"}
        self.order.save(update_fields=["selection"])
        self.photo_key = f"orders/{self.order.pk}/source/photo.jpg"
        self.storage.save(self.photo_key, BytesIO(b"photo-bytes"))
        self.photo = OrderPhoto.objects.create(order=self.order, storage_key=self.photo_key, original_filename="photo.jpg",
                                               mime_type="image/jpeg", size_bytes=11)
        self.preview_job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.SUCCEEDED,
            attempt=1, provider="fake", started_at=timezone.now(), finished_at=timezone.now(),
            input_metadata={"cost": {"cost_source": "CONFIG_SNAPSHOT", "cost_minor": 742, "billable": True}},
        )
        self.preview_key = f"generated/order-{self.order.pk}/preview/p.png"
        self.storage.save(self.preview_key, BytesIO(b"preview"))
        self.preview = GeneratedAsset.objects.create(order=self.order, job=self.preview_job, kind=GeneratedAsset.Kind.PREVIEW,
                                                     storage_key=self.preview_key, size_bytes=7)
        _job, self.final = self.add_final(self.order, "hello", attempt=1, content=b"final-normalized")
        self.original_key = f"generated/order-{self.order.pk}/final/original-hello.png"
        self.storage.save(self.original_key, BytesIO(b"final-original"))
        self.final.metadata = {"normalized_from": {"storage_key": self.original_key, "width": 1024}}
        self.final.save(update_fields=["metadata"])
        self.payment = Payment.objects.create(order=self.order, provider="yookassa", status=Payment.Status.CONFIRMED,
                                              amount_minor=10000, currency="RUB", external_payment_id="pay-1",
                                              confirmed_at=timezone.now())
        self.qc = QcReport.objects.create(order=self.order, attempt=1, status=QcReport.Status.PASSED, expected_count=1)
        self.closed_event = OrderEvent.objects.create(
            order=self.order, event_type=OrderEvent.Type.STATUS_CHANGED, from_status="delivery_in_progress",
            to_status=Order.Status.DELIVERED,
        )
        self.keys = [self.photo_key, self.preview_key, self.final.storage_key, self.original_key]

    def age(self, days):
        OrderEvent.objects.filter(pk=self.closed_event.pk).update(created_at=timezone.now() - timedelta(days=days))

    def files_present(self):
        return [self.storage.exists(key) for key in self.keys]

    def assert_accounting_intact(self):
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.preview_job.refresh_from_db()
        self.assertEqual(self.preview_job.input_metadata["cost"]["cost_minor"], 742)
        self.assertTrue(QcReport.objects.filter(pk=self.qc.pk).exists())
        self.assertTrue(OrderEvent.objects.filter(pk=self.closed_event.pk).exists())
        self.assertTrue(OrderPhoto.objects.filter(pk=self.photo.pk).exists(), "the row stays, the file goes")
        self.assertTrue(GeneratedAsset.objects.filter(pk=self.final.pk).exists())


class RetentionPolicyTests(LifecycleFixture):
    def test_default_policy_is_off_and_keeps_everything(self):
        with cfg(), env_off():
            policy = retention_policy()
            self.assertFalse(policy["enabled"])
            self.assertEqual(policy["days"], {KIND_SOURCE: None, KIND_PREVIEW: None, KIND_FINAL: None})
            self.age(400)
            plan = MediaLifecycleService(storage=self.storage).retention_plan()
        self.assertEqual(plan.items, [])

    def test_invalid_period_keeps_the_kind(self):
        with cfg(MEDIA_RETENTION_SOURCE_PHOTOS_DAYS="soon"), env_off():
            self.assertIsNone(retention_policy()["days"][KIND_SOURCE])
        with cfg(MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=-1), env_off():
            self.assertIsNone(retention_policy()["days"][KIND_SOURCE])

    def test_plan_lists_only_terminal_orders_older_than_the_period_per_kind(self):
        open_order = self.make_order(quantity=1, status=Order.Status.PAID)
        key = f"orders/{open_order.pk}/source/photo.jpg"
        self.storage.save(key, BytesIO(b"open"))
        OrderPhoto.objects.create(order=open_order, storage_key=key, size_bytes=4)
        Order.objects.filter(pk=open_order.pk).update(updated_at=timezone.now() - timedelta(days=400))
        with cfg(MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=30, MEDIA_RETENTION_PREVIEWS_DAYS=90), env_off():
            service = MediaLifecycleService(storage=self.storage)
            self.age(10)
            self.assertEqual(service.retention_plan().items, [], "younger than every period")
            self.age(45)
            plan = service.retention_plan()
            self.assertEqual([(i.kind, i.obj.pk) for i in plan.items], [(KIND_SOURCE, self.photo.pk)])
            self.age(100)
            plan = service.retention_plan()
            self.assertEqual(sorted(i.kind for i in plan.items), [KIND_PREVIEW, KIND_SOURCE])
            self.assertEqual(plan.counts(), {KIND_SOURCE: 1, KIND_PREVIEW: 1, KIND_FINAL: 0})
            self.assertEqual(plan.bytes, 18)
            self.assertNotIn(open_order.pk, [o.pk for o in plan.orders], "an open order is never planned")
        self.assertTrue(self.storage.exists(key))

    def test_apply_without_the_flag_is_refused_and_dry_run_deletes_nothing(self):
        self.age(400)
        with cfg(MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=1), env_off():
            service = MediaLifecycleService(storage=self.storage)
            plan = service.retention_plan()
            self.assertEqual(len(plan.items), 1)
            with self.assertRaisesMessage(MediaLifecycleError, "MEDIA_RETENTION_ENABLED is not true"):
                service.apply_retention(plan)
        self.assertEqual(self.files_present(), [True, True, True, True])
        self.assertFalse(OrderEvent.objects.filter(event_type=OrderEvent.MEDIA_PURGED).exists())

    def test_apply_deletes_planned_files_marks_rows_and_writes_the_event(self):
        self.age(400)
        with cfg(MEDIA_RETENTION_ENABLED="true", MEDIA_RETENTION_FINALS_DAYS=30), env_off():
            service = MediaLifecycleService(storage=self.storage)
            report = service.apply_retention(service.retention_plan())
        self.assertEqual(report, {"orders": 1, "files": 2, "bytes": len(b"final-normalized"),
                                  "counts": {KIND_SOURCE: 0, KIND_PREVIEW: 0, KIND_FINAL: 1}})
        self.assertEqual(self.files_present(), [True, True, False, False], "final + its provider original")
        self.final.refresh_from_db()
        purged = self.final.metadata["purged"]
        self.assertEqual(purged["rule"], "retention")
        self.assertTrue(purged["at"])
        self.assertTrue(is_purged(self.final))
        self.assertIn("normalized_from", self.final.metadata, "audit metadata stays")
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.SYSTEM)
        self.assertEqual(event.payload, {"rule": "retention", "reason": "retention policy",
                                         "counts": {KIND_SOURCE: 0, KIND_PREVIEW: 0, KIND_FINAL: 1},
                                         "files": 2, "bytes": len(b"final-normalized"), "contact_cleared": False})
        for pii in ("photo.jpg", "Анна", "anna", "storage_key"):
            self.assertNotIn(pii, str(event.payload))
        self.order.refresh_from_db()
        self.assertEqual(self.order.selection["contact"], "Анна, @anna", "retention never touches the contact")
        self.assert_accounting_intact()
        # a second run finds nothing left for that kind
        with cfg(MEDIA_RETENTION_ENABLED="true", MEDIA_RETENTION_FINALS_DAYS=30), env_off():
            self.assertEqual(service.retention_plan().items, [])

    def test_management_command_dry_run_by_default_and_apply_gate(self):
        self.age(400)
        out = StringIO()
        with cfg(MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=1, MEDIA_ROOT=self.storage.root), env_off():
            call_command("media_retention", stdout=out)
            text = out.getvalue()
            self.assertIn("enabled=False", text)
            self.assertIn("source_photos 1", text)
            self.assertIn("dry-run: nothing deleted", text)
            self.assertTrue(self.storage.exists(self.photo_key))
            with self.assertRaisesMessage(CommandError, "MEDIA_RETENTION_ENABLED is not true"):
                call_command("media_retention", "--apply", stdout=StringIO())
            self.assertTrue(self.storage.exists(self.photo_key))
        out = StringIO()
        with cfg(MEDIA_RETENTION_ENABLED="true", MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=1,
                               MEDIA_ROOT=self.storage.root), env_off():
            call_command("media_retention", "--apply", stdout=out)
        self.assertIn("applied: 1 order(s), 1 file(s), 11 bytes deleted", out.getvalue())
        self.assertFalse(self.storage.exists(self.photo_key))


class CustomerRequestTests(LifecycleFixture):
    def test_purge_order_deletes_all_media_clears_contact_and_keeps_accounting(self):
        result = MediaLifecycleService(storage=self.storage).purge_order(self.order, reason="запрос клиента в чате", actor_ref="root")
        self.assertEqual(result["counts"], {KIND_SOURCE: 1, KIND_PREVIEW: 1, KIND_FINAL: 1})
        self.assertEqual(result["files"], 4)
        self.assertTrue(result["contact_cleared"])
        self.assertEqual(self.files_present(), [False, False, False, False])
        self.order.refresh_from_db()
        self.assertEqual(self.order.selection["contact"], "")
        self.assertTrue(self.order.selection["contact_purged_at"])
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        self.photo.refresh_from_db()
        self.assertEqual(self.photo.metadata["purged"]["rule"], "customer_request")
        self.assertEqual(self.photo.metadata["purged"]["reason"], "запрос клиента в чате")
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload["rule"], "customer_request")
        self.assertTrue(event.payload["contact_cleared"])
        self.assert_accounting_intact()
        # idempotent: nothing to delete twice, no second event
        again = MediaLifecycleService(storage=self.storage).purge_order(self.order, reason="повтор", actor_ref="root")
        self.assertEqual(again["files"], 0)
        self.assertEqual(OrderEvent.objects.filter(order=self.order, event_type=OrderEvent.MEDIA_PURGED).count(), 1)

    def test_open_orders_and_empty_reasons_are_refused(self):
        with self.assertRaisesMessage(MediaLifecycleError, "reason is required"):
            MediaLifecycleService(storage=self.storage).purge_order(self.order, reason="  ", actor_ref="root")
        open_order = self.make_order(quantity=1, status=Order.Status.PREVIEW_REVIEW)
        with self.assertRaisesMessage(MediaLifecycleError, "never deleted"):
            MediaLifecycleService(storage=self.storage).purge_order(open_order, reason="x", actor_ref="root")
        self.assertEqual(self.files_present(), [True, True, True, True])


class ConsoleActionTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.superuser = User.objects.create_superuser(username="root", email="root@example.com", password="pass")
        self.staff = User.objects.create_user(username="op", email="op@example.com", password="pass", is_staff=True)
        from django.contrib.auth.models import Permission
        for perm in ("view_order", "change_order"):
            self.staff.user_permissions.add(Permission.objects.get(codename=perm))
        self.url = reverse("admin:core_order_purge_media", args=[self.order.pk])
        self.change_url = reverse("admin:core_order_change", args=[self.order.pk])

    def test_link_is_offered_for_terminal_orders_only(self):
        self.client.force_login(self.superuser)
        self.assertContains(self.client.get(self.change_url), "Удалить медиа клиента…")
        open_order = self.make_order(quantity=1, status=Order.Status.PAID)
        self.assertNotContains(self.client.get(reverse("admin:core_order_change", args=[open_order.pk])), "Удалить медиа клиента…")

    def test_superuser_confirms_with_a_reason_and_media_are_purged(self):
        self.client.force_login(self.superuser)
        with override_settings(MEDIA_ROOT=self.storage.root):
            page = self.client.get(self.url)
            self.assertContains(page, "Будут удалены с диска 3 файл(ов)")
            self.assertContains(page, 'name="reason"')
            response = self.client.post(self.url, {"reason": " "})
            self.assertContains(response, "Укажите причину удаления.")
            self.assertEqual(self.files_present(), [True, True, True, True])
            response = self.client.post(self.url, {"reason": "клиент попросил удалить фото"}, follow=True)
        self.assertContains(response, "Медиа клиента удалены: фото 1, превью 1, финалы 1 (4 файл(ов)")
        self.assertContains(response, "контакт стёрт")
        self.assertEqual(self.files_present(), [False, False, False, False])
        self.assert_accounting_intact()
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_ref, "root")

    def test_staff_without_superuser_is_refused(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"reason": "x"}, follow=True)
        self.assertContains(response, "только суперпользователь")
        self.assertEqual(self.files_present(), [True, True, True, True])

    def test_open_order_is_refused_in_the_console(self):
        self.client.force_login(self.superuser)
        open_order = self.make_order(quantity=1, status=Order.Status.PAID)
        response = self.client.post(reverse("admin:core_order_purge_media", args=[open_order.pk]), {"reason": "x"}, follow=True)
        self.assertContains(response, "только у завершённых заказов")


class AccessTests(LifecycleFixture):
    def test_file_views_require_a_logged_in_staff_user(self):
        """Anonymous → the admin login redirect, never the file (staff access
        itself: tests_media.test_staff_can_retrieve_saved_file_through_admin)."""
        for url in (reverse("admin:core_orderphoto_file", args=[self.photo.pk]),
                    reverse("admin:core_preview_asset_file", args=[self.final.pk])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login/", response["Location"])
