"""DRF-2053: Production Console actions "Deliver final set" / "Resume delivery".

The delivery surface is layered on the QC console (DRF-2052): one Order
ModelAdmin is registered, QC actions stay available, delivery actions
appear only once the QC gate accepts the current set.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.final_delivery_console import CONSOLE_MAX_ITEMS, FinalDeliveryOrderAdmin
from apps.core.models import (
    ChannelIdentity,
    FinalDelivery,
    GeneratedAsset,
    GenerationJob,
    Order,
    Product,
    QcReport,
    Style,
    User,
)
from apps.core.qc_console import QcOrderAdmin
from apps.core.services.final_delivery import FinalDeliveryService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_final_delivery import FakeAdapter, FinalDeliveryTestCase, product_config

READY_FOR_DELIVERY = Order.Status.READY_FOR_DELIVERY
PILOT9 = ["hello", "bye", "thanks", "great", "no", "love", "laugh", "angry", "surprised"]


class ProductionConsoleFinalDeliveryTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()

        self.admin = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password="pass"
        )
        self.client.force_login(self.admin)

        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="100"
        )
        product = Product.objects.create(code="pack9", name="Pack", config=product_config(9))
        style = Style.objects.create(code="comic", name="Comic")
        self.order = Order.objects.create(
            user=user,
            channel_identity=identity,
            product=product,
            style=style,
            status=READY_FOR_DELIVERY,
            selection={"emotions": PILOT9},
        )
        for attempt, slot in enumerate(PILOT9, start=1):
            job = GenerationJob.objects.create(
                order=self.order,
                task_type=GenerationJob.TaskType.FULL,
                status=GenerationJob.Status.SUCCEEDED,
                attempt=attempt,
                slot_key=slot,
                provider="fake",
            )
            key = f"generated/order-{self.order.pk}/final/{slot}.png"
            self.storage.save(key, BytesIO(slot.encode()))
            GeneratedAsset.objects.create(
                order=self.order,
                job=job,
                kind=GeneratedAsset.Kind.FINAL,
                slot_key=slot,
                storage_key=key,
                size_bytes=len(slot),
            )
        FinalDeliveryTestCase.pass_qc(self.order)
        self.adapter = FakeAdapter()
        self.service = FinalDeliveryService(adapter=self.adapter, storage=self.storage)

    def _patched(self):
        return patch.object(
            FinalDeliveryOrderAdmin, "get_final_delivery_service", return_value=self.service
        )

    def test_change_page_shows_deliver_action_and_slot_report(self):
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deliver final set")
        self.assertContains(response, reverse("admin:core_order_deliver_final", args=[self.order.pk]))
        self.assertNotContains(response, "Resume delivery")
        self.assertContains(response, "hello · pending")

    def test_deliver_requires_confirmation_then_sends_batch(self):
        url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        with self._patched():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deliver final set")
        self.assertEqual(self.adapter.items, [])

        with self._patched():
            response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.adapter.items), CONSOLE_MAX_ITEMS)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERY_IN_PROGRESS)
        self.assertContains(response, "Resume delivery")
        self.assertContains(response, "hello · sent")
        self.assertContains(response, "message=msg-1")
        self.assertContains(response, "great · pending")

    def test_resume_completes_delivery_without_duplicates(self):
        deliver_url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        resume_url = reverse("admin:core_order_resume_final_delivery", args=[self.order.pk])
        with self._patched():
            self.client.post(deliver_url, follow=True)
            self.client.post(resume_url, follow=True)
            response = self.client.post(resume_url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.adapter.items), 9)
        sent = [item["filename"].split("-", 2)[2].rsplit(".", 1)[0] for item in self.adapter.items]
        self.assertEqual(sent, PILOT9)
        self.assertEqual(len(self.adapter.summaries), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        self.assertContains(response, "Final set delivered")
        self.assertNotContains(response, "Resume delivery")
        self.assertNotContains(response, "Deliver final set")

    def test_failed_slot_is_reported_and_resume_retries_only_it(self):
        self.adapter.fail_slots = {"bye"}
        self.adapter.status_code = 502
        deliver_url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        resume_url = reverse("admin:core_order_resume_final_delivery", args=[self.order.pk])
        with self._patched():
            response = self.client.post(deliver_url, follow=True)
        self.assertContains(response, "bye · failed")
        self.assertContains(response, "retryable: send failed for bye")
        self.assertContains(response, "сбой на slots bye")
        self.assertEqual(len(self.adapter.items), 2)

        with self._patched():
            response = self.client.post(resume_url, follow=True)
        sent = [item["filename"].split("-", 2)[2].rsplit(".", 1)[0] for item in self.adapter.items]
        self.assertEqual(sent, ["hello", "thanks", "bye", "great", "no"])
        self.assertContains(response, "bye · sent")
        self.assertEqual(FinalDelivery.objects.filter(order=self.order).count(), 2)

    def test_deliver_is_rejected_outside_ready_for_delivery(self):
        self.order.status = Order.Status.QUALITY_CONTROL
        self.order.save(update_fields=["status"])
        url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        with self._patched():
            response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "только из READY_FOR_DELIVERY")
        self.assertEqual(self.adapter.items, [])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)

    def test_resume_before_start_reports_service_error(self):
        url = reverse("admin:core_order_resume_final_delivery", args=[self.order.pk])
        with self._patched():
            response = self.client.post(url, follow=True)
        self.assertContains(response, "use deliver()")
        self.assertEqual(self.adapter.items, [])

    def test_incomplete_set_is_shown_and_blocks_delivery(self):
        GeneratedAsset.objects.filter(order=self.order, slot_key="laugh").delete()
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertContains(response, "laugh · pending · NO CURRENT ASSET")
        # The QC PASS no longer matches the set, so no action is offered ...
        self.assertContains(response, "QC gate:")
        self.assertNotContains(response, reverse("admin:core_order_deliver_final", args=[self.order.pk]))
        # ... and a direct POST is rejected by the gate before any send.
        url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        with self._patched():
            response = self.client.post(url, follow=True)
        self.assertContains(response, "changed after QC PASS")
        self.assertEqual(self.adapter.items, [])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, READY_FOR_DELIVERY)

    def test_without_qc_pass_no_action_and_post_is_rejected(self):
        QcReport.objects.filter(order=self.order).delete()
        change_url = reverse("admin:core_order_change", args=[self.order.pk])
        deliver_url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        with self._patched():
            response = self.client.get(change_url)
        self.assertContains(response, "QC gate:")
        self.assertContains(response, "DELIVERY: заблокирован")  # QC panel, inherited
        self.assertNotContains(response, deliver_url)
        with self._patched():
            response = self.client.post(deliver_url, follow=True)
        self.assertContains(response, "no passed QC report")
        self.assertEqual(self.adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=self.order).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, READY_FOR_DELIVERY)

    def test_delivered_order_offers_no_send_action_and_post_sends_nothing(self):
        deliver_url = reverse("admin:core_order_deliver_final", args=[self.order.pk])
        resume_url = reverse("admin:core_order_resume_final_delivery", args=[self.order.pk])
        with self._patched():
            self.service.deliver(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
        sent_before = len(self.adapter.items)
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
            self.assertNotContains(response, deliver_url)
            self.assertNotContains(response, resume_url)
            self.client.post(deliver_url, follow=True)
            self.client.post(resume_url, follow=True)
        self.assertEqual(len(self.adapter.items), sent_before)
        self.assertEqual(len(self.adapter.summaries), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)

    # ------------------------------------------------ QC console integration

    def test_single_order_admin_registration_is_qc_console_subclass(self):
        registered = admin.site._registry[Order]
        self.assertIsInstance(registered, FinalDeliveryOrderAdmin)
        self.assertIsInstance(registered, QcOrderAdmin)
        self.assertEqual(
            sum(1 for model in admin.site._registry if model is Order), 1
        )
        self.assertIn("qc_panel", registered.fields)
        self.assertIn("final_delivery_panel", registered.fields)
        self.assertIn("qc_panel", registered.readonly_fields)
        self.assertIn("final_delivery_panel", registered.readonly_fields)

    def test_qc_and_delivery_actions_coexist_on_change_page(self):
        # QC URLs (DRF-2052) and delivery URLs (DRF-2053) resolve side by side.
        qc_urls = [
            reverse("admin:core_order_qc_start", args=[self.order.pk]),
            reverse("admin:core_order_qc_finalize", args=[self.order.pk]),
            reverse("admin:core_order_qc_retry", args=[self.order.pk, "hello"]),
        ]
        delivery_urls = [
            reverse("admin:core_order_deliver_final", args=[self.order.pk]),
            reverse("admin:core_order_resume_final_delivery", args=[self.order.pk]),
        ]
        self.assertTrue(all(qc_urls) and all(delivery_urls))
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertContains(response, "QC — финальные стикеры")
        self.assertContains(response, "DELIVERY: разрешён (QC PASS)")
        self.assertContains(response, "Final delivery — набор клиенту")
        self.assertContains(response, delivery_urls[0])

    def test_qc_start_action_still_works_through_delivery_admin(self):
        # An order still in QC shows the QC action and no delivery action.
        self.order.status = Order.Status.QUALITY_CONTROL
        self.order.save(update_fields=["status"])
        QcReport.objects.filter(order=self.order).delete()
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertContains(response, reverse("admin:core_order_qc_start", args=[self.order.pk]))
        self.assertContains(response, "Открыть QC report")
        self.assertContains(response, "Доставка доступна после QC PASS")
        self.assertNotContains(response, reverse("admin:core_order_deliver_final", args=[self.order.pk]))
        with self._patched():
            response = self.client.post(
                reverse("admin:core_order_qc_start", args=[self.order.pk]), follow=True
            )
        self.assertContains(response, "QC attempt 1 открыт")
        self.assertEqual(QcReport.objects.filter(order=self.order).count(), 1)

    def test_quantity_mismatch_is_shown_as_set_error(self):
        self.order.selection = {"emotions": PILOT9[:8]}
        self.order.save(update_fields=["selection"])
        with self._patched():
            response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertContains(response, "Ошибка набора")
        self.assertContains(response, "does not match product quantity 9")

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "1:test", "MAX_BOT_TOKEN": "max-test"})
    def test_channel_routing_builds_matching_adapter(self):
        admin_instance = FinalDeliveryOrderAdmin(Order, None)
        self.assertEqual(
            admin_instance.get_final_delivery_service(self.order).adapter.channel, "telegram"
        )
        max_user = User.objects.create()
        max_identity = ChannelIdentity.objects.create(
            user=max_user, channel=ChannelIdentity.Channel.MAX, external_user_id="m-1"
        )
        max_order = Order.objects.create(
            user=max_user,
            channel_identity=max_identity,
            product=self.order.product,
            style=self.order.style,
            status=READY_FOR_DELIVERY,
        )
        self.assertEqual(admin_instance.get_final_delivery_service(max_order).adapter.channel, "max")
