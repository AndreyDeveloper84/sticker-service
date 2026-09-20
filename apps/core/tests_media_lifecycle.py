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
    REASON_CUSTOMER_REQUEST,
    REASON_LEGAL,
    MediaLifecycleError,
    MediaLifecycleService,
    error_text,
    is_purged,
    mask_note,
    media_items,
    purge_failed,
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
        self.assertEqual(report, {"orders": 1, "files": 2, "bytes": len(b"final-normalized"), "errors": 0,
                                  "remaining": 0, "status": "complete",
                                  "counts": {KIND_SOURCE: 0, KIND_PREVIEW: 0, KIND_FINAL: 1}})
        self.assertEqual(self.files_present(), [True, True, False, False], "final + its provider original")
        self.final.refresh_from_db()
        purged = self.final.metadata["purged"]
        self.assertEqual(purged["rule"], "retention")
        self.assertEqual(purged["attempts"], 1)
        self.assertNotIn("reason", purged, "free text never lands in the row")
        self.assertTrue(purged["at"])
        self.assertTrue(is_purged(self.final))
        self.assertIn("normalized_from", self.final.metadata, "audit metadata stays")
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.SYSTEM)
        self.assertEqual(event.payload, {"rule": "retention", "reason": "retention", "reason_note": "",
                                         "status": "complete",
                                         "counts": {KIND_SOURCE: 0, KIND_PREVIEW: 0, KIND_FINAL: 1},
                                         "files": 2, "bytes": len(b"final-normalized"), "errors": 0,
                                         "remaining": 0, "contact_cleared": False})
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
        result = MediaLifecycleService(storage=self.storage).purge_order(
            self.order, reason=REASON_CUSTOMER_REQUEST, note="написала в чате anna@example.com, +7 900 000-00-00",
            actor_ref="root",
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["remaining"], 0)
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
        self.assertEqual(self.photo.metadata["purged"]["reason_ref"], "customer_request")
        self.assertNotIn("reason", self.photo.metadata["purged"])
        self.assertEqual(self.photo.original_filename, "", "the customer's file name goes with the file")
        self.assertEqual(self.photo.storage_key, self.photo_key, "the key stays: idempotency + audit")
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload["rule"], "customer_request")
        self.assertEqual(event.payload["reason"], "customer_request")
        self.assertEqual(event.payload["reason_note"], "написала в чате ***, ***", "the note is masked, event-only")
        self.assertEqual(event.payload["status"], "complete")
        self.assertTrue(event.payload["contact_cleared"])
        for pii in ("anna@", "+7 900", "photo.jpg", "storage_key"):
            self.assertNotIn(pii, str(event.payload))
        self.assert_accounting_intact()
        # nothing to delete twice: no second event
        again = MediaLifecycleService(storage=self.storage).purge_order(self.order, reason=REASON_LEGAL, actor_ref="root")
        self.assertEqual(again["files"], 0)
        self.assertEqual(OrderEvent.objects.filter(order=self.order, event_type=OrderEvent.MEDIA_PURGED).count(), 1)

    def test_file_system_error_on_one_file_does_not_roll_back_the_others(self):
        """Owner review of #96: a file that could not be deleted must NEVER be
        marked purged (it stayed on disk, left every future plan, and the
        operator was told «удалено»). Now: purge_failed on the row, the object
        stays in the plan, the run is «partial», the next run retries it."""
        real_delete = self.storage.delete
        broken = {self.preview_key}

        def flaky_delete(key):
            if key in broken:
                raise PermissionError(13, "Permission denied", "/srv/media/" + key)
            return real_delete(key)

        service = MediaLifecycleService(storage=self.storage)
        with mock.patch.object(self.storage, "delete", side_effect=flaky_delete), \
                self.assertLogs("apps.core.services.media_lifecycle", "WARNING") as logs:
            result = service.purge_order(self.order, reason=REASON_CUSTOMER_REQUEST, actor_ref="root")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(result["counts"], {KIND_SOURCE: 1, KIND_PREVIEW: 0, KIND_FINAL: 1})
        self.assertEqual(self.files_present(), [False, True, False, False])
        self.assertTrue(any("file delete failed (PermissionError) — will be retried" in line for line in logs.output))
        self.preview.refresh_from_db()
        self.assertFalse(is_purged(self.preview), "a file still on disk is never «purged»")
        failed = purge_failed(self.preview)
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(failed["last_error"], "PermissionError: Permission denied")
        self.assertNotIn("/srv", failed["last_error"])
        self.assertNotIn(self.preview_key, str(failed))
        self.assertEqual(failed["rule"], "customer_request")
        self.assertTrue(failed["last_attempt_at"])
        for obj in (self.photo, self.final):
            obj.refresh_from_db()
            self.assertTrue(is_purged(obj))
        self.assertEqual([i.obj.pk for i in media_items(self.order)], [self.preview.pk], "still in the plan")
        events = list(OrderEvent.objects.filter(order=self.order, event_type=OrderEvent.MEDIA_PURGED).order_by("pk"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["status"], "partial")
        self.assertEqual(events[0].payload["remaining"], 1)
        self.assertEqual(events[0].payload["errors"], 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.selection["contact"], "")
        self.assert_accounting_intact()

        # the retry (file system healed) deletes it → purged with attempts=2, a new complete event
        broken.clear()
        result = service.purge_order(self.order, reason=REASON_CUSTOMER_REQUEST, actor_ref="root")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(result["counts"], {KIND_SOURCE: 0, KIND_PREVIEW: 1, KIND_FINAL: 0})
        self.assertEqual(self.files_present(), [False, False, False, False])
        self.preview.refresh_from_db()
        self.assertTrue(is_purged(self.preview))
        self.assertIsNone(purge_failed(self.preview))
        self.assertEqual(self.preview.metadata["purged"]["attempts"], 2)
        self.assertEqual(media_items(self.order), [])
        events = list(OrderEvent.objects.filter(order=self.order, event_type=OrderEvent.MEDIA_PURGED).order_by("pk"))
        self.assertEqual([e.payload["status"] for e in events], ["partial", "complete"])

    def test_retention_retries_a_failed_file_on_the_next_run(self):
        self.age(400)
        real_delete = self.storage.delete
        with cfg(MEDIA_RETENTION_ENABLED="true", MEDIA_RETENTION_SOURCE_PHOTOS_DAYS=1), env_off():
            service = MediaLifecycleService(storage=self.storage)
            with mock.patch.object(self.storage, "delete", side_effect=OSError(5, "I/O error")):
                report = service.apply_retention(service.retention_plan())
            self.assertEqual((report["status"], report["remaining"], report["files"]), ("partial", 1, 0))
            self.assertTrue(self.storage.exists(self.photo_key))
            plan = service.retention_plan()
            self.assertEqual([i.obj.pk for i in plan.items], [self.photo.pk], "planned again")
            report = service.apply_retention(plan)
            self.assertEqual((report["status"], report["remaining"], report["files"]), ("complete", 0, 1))
        self.assertFalse(self.storage.exists(self.photo_key))
        self.photo.refresh_from_db()
        self.assertEqual(self.photo.metadata["purged"]["attempts"], 2)
        self.assertEqual(self.photo.original_filename, "")

    def test_note_masking_and_error_text_carry_no_pii(self):
        self.assertEqual(mask_note("Анна anna@example.com +7 900 000-00-00 @anna_ivanova"), "Анна *** *** ***")
        self.assertEqual(len(mask_note("x" * 300)), 120)
        self.assertEqual(mask_note("  a   b  "), "a b")
        self.assertEqual(error_text(PermissionError(13, "Permission denied", "D:\\media\\orders\\1\\photo.jpg")),
                         "PermissionError: Permission denied")
        self.assertEqual(error_text(OSError("[Errno 13] Permission denied: '/srv/media/orders/1/photo.jpg'")),
                         "OSError: [Errno 13] Permission denied: <path>")

    def test_storage_delete_refuses_empty_keys_directories_and_escapes(self):
        for key in ("", "   ", ".", "orders", f"orders/{self.order.pk}/source", "../outside"):
            with self.assertRaises(ValueError, msg=key):
                self.storage.delete(key)
        self.assertTrue(self.storage.root.exists(), "the root is never unlinked")
        self.assertEqual(self.files_present(), [True, True, True, True])
        self.assertFalse(self.storage.delete(f"orders/{self.order.pk}/source/missing.jpg"))
        self.assertTrue(self.storage.delete(self.photo_key))
        self.assertFalse(self.storage.exists(self.photo_key))

    def test_open_orders_and_empty_reasons_are_refused(self):
        for reason in ("", "  ", "запрос клиента в чате"):
            with self.assertRaisesMessage(MediaLifecycleError, "reason code is required"):
                MediaLifecycleService(storage=self.storage).purge_order(self.order, reason=reason, actor_ref="root")
        open_order = self.make_order(quantity=1, status=Order.Status.PREVIEW_REVIEW)
        with self.assertRaisesMessage(MediaLifecycleError, "never deleted"):
            MediaLifecycleService(storage=self.storage).purge_order(open_order, reason=REASON_LEGAL, actor_ref="root")
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
            self.assertContains(page, '<select id="reason" name="reason" required>')
            self.assertContains(page, '<option value="customer_request">запрос клиента</option>')
            self.assertContains(page, 'name="note"')
            for bad in (" ", "клиент попросил удалить фото"):
                response = self.client.post(self.url, {"reason": bad})
                self.assertContains(response, "Выберите причину удаления.")
            self.assertEqual(self.files_present(), [True, True, True, True])
            response = self.client.post(self.url, {"reason": "customer_request", "note": "написала @anna_ivanova"}, follow=True)
        self.assertContains(response, "Медиа клиента удалены: фото 1, превью 1, финалы 1 (4 файл(ов)")
        self.assertContains(response, "контакт стёрт")
        self.assertNotContains(response, "частично")
        self.assertEqual(self.files_present(), [False, False, False, False])
        self.assert_accounting_intact()
        event = OrderEvent.objects.get(order=self.order, event_type=OrderEvent.MEDIA_PURGED)
        self.assertEqual(event.actor_ref, "root")
        self.assertEqual(event.payload["reason_note"], "написала ***")

    def test_partial_purge_is_a_warning_and_the_retry_completes_it(self):
        self.client.force_login(self.superuser)
        real_delete = self.storage.delete
        broken = {self.final.storage_key}

        def flaky_delete(key):
            if key in broken:
                raise PermissionError(13, "Permission denied")
            return real_delete(key)

        with override_settings(MEDIA_ROOT=self.storage.root), \
                mock.patch("apps.core.production_console.MediaLifecycleService",
                           return_value=MediaLifecycleService(storage=self.storage)), \
                mock.patch.object(self.storage, "delete", side_effect=flaky_delete):
            response = self.client.post(self.url, {"reason": "customer_request"}, follow=True)
            self.assertContains(response, "Медиа клиента удалены частично")
            self.assertContains(response, "осталось 1 файл(ов)")
            self.assertContains(response, "повторите действие")
            self.assertNotContains(response, "Медиа клиента удалены:")
            self.assertContains(response, "Удалить медиа клиента…")  # the action stays offered
            self.assertContains(response, "1 файл(ов)")  # 1 remaining in the explanation
            page = self.client.get(self.url)
            self.assertContains(page, "1 файл(ов) не удалось удалить в прошлый раз — будет повторная попытка")
        self.assertTrue(self.storage.exists(self.final.storage_key))
        self.final.refresh_from_db()
        self.assertFalse(is_purged(self.final))
        with override_settings(MEDIA_ROOT=self.storage.root), \
                mock.patch("apps.core.production_console.MediaLifecycleService",
                           return_value=MediaLifecycleService(storage=self.storage)):
            response = self.client.post(self.url, {"reason": "customer_request"}, follow=True)
        self.assertContains(response, "Медиа клиента удалены: фото 0, превью 0, финалы 1 (1 файл(ов)")  # the original went in run 1
        self.assertEqual(self.files_present(), [False, False, False, False])
        statuses = [e.payload["status"] for e in OrderEvent.objects.filter(order=self.order, event_type=OrderEvent.MEDIA_PURGED).order_by("pk")]
        self.assertEqual(statuses, ["partial", "complete"])

    def test_staff_without_superuser_is_refused(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"reason": "customer_request"}, follow=True)
        self.assertContains(response, "только суперпользователь")
        self.assertEqual(self.files_present(), [True, True, True, True])

    def test_open_order_is_refused_in_the_console(self):
        self.client.force_login(self.superuser)
        open_order = self.make_order(quantity=1, status=Order.Status.PAID)
        response = self.client.post(reverse("admin:core_order_purge_media", args=[open_order.pk]), {"reason": "customer_request"}, follow=True)
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
