"""DRF-2084: Production Console in Russian, explicit QC checklist, «Следующий шаг».

Live evidence (orders 10/11): the operator submitted the QC checklist with
no boxes ticked and got FAIL on all six criteria; button/status/panel names
mixed English and Russian; the next action was not obvious. These tests
render the console through the registered admin and pin the contract:
no English labels from the old console, every status has exactly one main
button (or a waiting text), and an incomplete checklist is a validation
error that creates no report outcome.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.console_text import QC_CRITERIA, humanize_error
from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderPhoto,
    Payment,
    Product,
    QcReport,
    Revision,
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.full_production import FullProductionError, FullProductionService
from apps.core.services.qc import HUMAN_CRITERIA, QcError, QcService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_final_delivery import FakeAdapter
from apps.core.tests_qc import make_image

EMOTIONS = [{"code": "hello", "label": "Привет"}, {"code": "bye", "label": "Пока"}]
PACK2 = {"kind": "pack", "quantity": 2, "emotion_count": 2, "emotions": EMOTIONS, "price_minor": 50000}

# Labels of the old, mixed-language console that must never render again.
OLD_ENGLISH_LABELS = [
    "Start / Resume Full Production",
    "Retry Failed Slots",
    "Regenerate Slots…",
    "Generate / Retry Preview",
    "Regenerate Preview",
    "Generate / Retry Revision",
    "Approve this preview",
    "Deliver preview",
    "Deliver final set",
    "Resume delivery",
    "Production plan (full generation)",
    "Preview — действия",
    "Preview assets",
    "Generation jobs",
    "Final delivery — набор клиенту",
    "QC — финальные стикеры",
    "Открыть QC report",
    "Pack generating",
    "Quality control",
    "Ready for delivery",
    "Internal preview review",
]


class StickerProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


class ConsoleRuTestCase(TestCase):
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
        self.identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.MAX, external_user_id="10", display_name="Борис"
        )
        self.product = Product.objects.create(code="pack2", name="Стикерпак — 2", config=PACK2)
        self.style = Style.objects.create(code="comic", name="Комикс")
        self.order = Order.objects.create(
            user=user, channel_identity=self.identity, product=self.product, style=self.style,
            status=Order.Status.PAID, selection={"emotions": ["hello", "bye"]},
        )
        photo_key = f"orders/{self.order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=photo_key, original_filename="p.jpg", mime_type="image/jpeg", size_bytes=5)
        Payment.objects.create(
            order=self.order, provider="yookassa", status=Payment.Status.CONFIRMED,
            amount_minor=50000, currency="RUB", external_payment_id="yk-1", confirmed_at=timezone.now(),
        )
        provider, storage = StickerProvider(), self.storage
        patcher = patch.object(
            ProductionOrderAdmin, "get_full_production_service",
            lambda self_: FullProductionService(provider=provider, storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        notice = patch.object(ProductionOrderAdmin, "notify_production_started", return_value=False)
        notice.start()
        self.addCleanup(notice.stop)

    # -- fixtures ---------------------------------------------------------------

    def add_preview(self, *, internal_approved=False, sent=False, customer_approved=False):
        job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED, attempt=1, provider="fake",
        )
        key = f"generated/order-{self.order.pk}/preview/{job.pk}.png"
        self.storage.save(key, BytesIO(make_image()))
        metadata = {}
        if internal_approved:
            metadata["internal_approved"] = True
        if sent:
            metadata["deliveries"] = [{"channel": "max", "status": "sent", "message_id": "m1"}]
        if customer_approved:
            metadata["customer_approved"] = True
        return GeneratedAsset.objects.create(
            order=self.order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key=key,
            size_bytes=10, metadata=metadata,
        )

    def set_status(self, status):
        Order.objects.filter(pk=self.order.pk).update(status=status)
        self.order.refresh_from_db()

    def produce(self):
        self.add_preview(internal_approved=True, sent=True, customer_approved=True)
        self.set_status(Order.Status.PREVIEW_REVIEW)
        FullProductionService(provider=StickerProvider(), storage=self.storage).start(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)

    def change_page(self):
        response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def url(self, name, *args):
        return reverse(f"admin:{name}", args=[self.order.pk, *args])


class RussianRenderingTests(ConsoleRuTestCase):
    def test_change_page_has_no_english_console_labels_in_every_status(self):
        self.add_preview()
        for status in Order.Status:
            with self.subTest(status=status):
                self.set_status(status)
                content = self.change_page()
                for old in OLD_ENGLISH_LABELS:
                    self.assertNotIn(old, content, f"{old!r} rendered in {status}")
                self.assertIn("Что делать сейчас", content)
                self.assertIn("Дополнительно", content)
                self.assertIn(str(Order.Status(status).label), content)

    def test_list_page_is_russian_with_status_badge_and_filters(self):
        response = self.client.get(reverse("admin:core_order_changelist"))
        content = response.content.decode()
        for text in ("Канал", "Клиент", "Продукт", "Стиль", "Статус", "Фото", "Оплачен", "Борис (10)"):
            self.assertIn(text, content)
        self.assertIn("статус", content)  # filter title
        self.assertIn("канал", content)
        self.assertIn("border-radius:10px", content)  # colored badge
        response = self.client.get(reverse("admin:core_order_changelist"), {"status": Order.Status.DELIVERED})
        self.assertNotIn(f">#{self.order.pk}<", response.content.decode())

    def test_confirmation_pages_are_russian_with_wait_and_cost_warning(self):
        for name, args in (
            ("core_order_generate_preview", ()),
            ("core_order_start_full_production", ()),
            ("core_order_retry_failed_production", ()),
            ("core_order_regenerate_slots", ()),
            ("core_order_force_retry_slot", ("hello",)),
        ):
            with self.subTest(name=name):
                content = self.client.get(self.url(name, *args)).content.decode()
                self.assertIn("Генерация занимает ~1–1.5 мин, не закрывайте страницу.", content)
                self.assertIn("платный вызов", content)
                self.assertIn("Подтвердить", content)
                self.assertIn(f"Заказ #{self.order.pk}", content)
        content = self.client.get(self.url("core_order_force_retry_slot", "hello")).content.decode()
        self.assertIn("Принудительный повтор слота «Привет»", content)
        content = self.client.get(self.url("core_order_regenerate_slots")).content.decode()
        self.assertIn("<code>hello</code> — «Привет»", content)

    def test_domain_errors_are_shown_in_russian(self):
        self.assertEqual(humanize_error(FullProductionError("No production slots requested")), "Не выбраны слоты для перегенерации.")
        self.assertEqual(
            humanize_error(FullProductionError("Order #5 cannot run full production from quality_control")),
            "Производство нельзя запустить из статуса «Контроль качества».",
        )
        self.assertEqual(
            humanize_error(QcError("Delivery is forbidden before QC PASS (order #5: quality_control)")),
            "Доставка запрещена до прохождения контроля качества.",
        )
        self.assertEqual(humanize_error(RuntimeError("weird")), "Ошибка: weird")
        # through the console: regenerate with an empty slot list
        self.produce()
        self.set_status(Order.Status.PACK_GENERATING)
        response = self.client.post(self.url("core_order_regenerate_slots"), {"slot_keys": ""}, follow=True)
        self.assertContains(response, "Не выбраны слоты для перегенерации.")
        self.assertNotContains(response, "No production slots requested")

    def test_panels_use_human_readable_rows(self):
        self.produce()
        content = self.change_page()
        self.assertIn("Слот «Привет» · готов · попыток: 1 · текущий файл #", content)
        self.assertIn("512×512", content)
        self.assertIn("попытка 1 · производство · готов · fake", content)
        self.assertIn("Ожидается стикеров: 2 · Готово: 2", content)


class NextStepTests(ConsoleRuTestCase):
    def expect(self, headline, button=None, url_name=None, *args):
        content = self.change_page()
        self.assertIn(headline, content)
        if button:
            self.assertIn(button, content)
            self.assertIn(self.url(url_name, *args), content)
        return content

    def test_waiting_for_customer_statuses_have_no_button(self):
        for status in (Order.Status.AWAITING_PHOTOS, Order.Status.READY_FOR_CHECKOUT, Order.Status.AWAITING_PAYMENT):
            with self.subTest(status=status):
                self.set_status(status)
                content = self.expect("Ожидаем клиента")
                self.assertNotIn('class="button default"', content)

    def test_paid_offers_generate_preview(self):
        self.expect("Сгенерировать превью", "Сгенерировать превью", "core_order_generate_preview")

    def test_internal_review_offers_approve_then_deliver(self):
        preview = self.add_preview()
        self.set_status(Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.expect("Проверьте превью и одобрите", f"Одобрить превью #{preview.pk}", "core_order_approve_preview", preview.pk)
        preview.metadata = {"internal_approved": True}
        preview.save(update_fields=["metadata"])
        self.expect("Отправить превью клиенту", "Отправить превью клиенту", "core_order_deliver_preview")

    def test_preview_review_waits_then_offers_production_after_customer_approval(self):
        preview = self.add_preview(internal_approved=True, sent=True)
        self.set_status(Order.Status.PREVIEW_REVIEW)
        content = self.expect("Ожидаем ответ клиента")
        self.assertNotIn(self.url("core_order_start_full_production"), content)
        preview.metadata = {**preview.metadata, "customer_approved": True}
        preview.save(update_fields=["metadata"])
        self.expect("Запустить производство", "Запустить производство", "core_order_start_full_production")

    def test_revision_requested_offers_generate_revision(self):
        source = self.add_preview(internal_approved=True, sent=True)
        Revision.objects.create(order=self.order, source_preview=source, category=Revision.Category.HAIR, customer_text="кудри")
        self.set_status(Order.Status.REVISION_REQUESTED)
        content = self.expect("Сгенерировать правку", "Сгенерировать правку", "core_order_generate_revision")
        self.assertIn("что исправить: <strong>Волосы</strong>", content)

    def test_pack_generating_offers_start_then_regenerate_for_qc_retry(self):
        self.add_preview(internal_approved=True, sent=True, customer_approved=True)
        self.set_status(Order.Status.PACK_GENERATING)
        self.expect("Запустить производство", "Запустить производство", "core_order_start_full_production")
        # QC retry pending → the single main button is the slot regeneration
        FullProductionService(provider=StickerProvider(), storage=self.storage).start(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        report = QcService(storage=self.storage).start_qc(order=self.order)
        checklist = {c: True for c in HUMAN_CRITERIA}
        checklist["crop"] = False
        failed = QcService(storage=self.storage).finalize_report(report=report, checklist=checklist)
        QcService(storage=self.storage).request_retry(report=failed, slot_keys=["hello"])
        self.order.refresh_from_db()
        content = self.expect("Перегенерировать слот «Привет»", "Перегенерировать слот «Привет»", "core_order_regenerate_slots")
        self.assertIn("?slots=hello", content)

    def test_quality_control_offers_open_then_fill_checklist(self):
        self.produce()
        self.expect("Открыть контроль качества", "Открыть QC-отчёт", "core_order_qc_start")
        QcService(storage=self.storage).start_qc(order=self.order)
        self.expect("Заполните чек-лист контроля качества", "Заполнить чек-лист", "core_order_qc_finalize")

    def test_delivery_statuses(self):
        self.produce()
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=self.order)
        qc.finalize_report(report=report, checklist={c: True for c in HUMAN_CRITERIA})
        self.order.refresh_from_db()
        self.expect("Отправить набор клиенту", "Отправить набор клиенту", "core_order_deliver_final")
        self.set_status(Order.Status.DELIVERY_IN_PROGRESS)
        self.expect("Продолжить доставку", "Продолжить доставку", "core_order_resume_final_delivery")
        self.set_status(Order.Status.DELIVERED)
        content = self.expect("Готово")
        self.assertNotIn('class="button default"', content)

    def test_secondary_actions_carry_cost_warnings(self):
        self.add_preview(internal_approved=True, sent=True, customer_approved=True)
        self.set_status(Order.Status.PACK_GENERATING)
        content = self.change_page()
        self.assertIn("Второстепенные действия", content)
        self.assertIn("Повторить неудавшиеся слоты", content)
        self.assertIn("Перегенерировать слоты…", content)
        self.assertIn("1 платный вызов на каждый слот", content)


class QcChecklistTests(ConsoleRuTestCase):
    def setUp(self):
        super().setUp()
        self.produce()
        self.report = QcService(storage=self.storage).start_qc(order=self.order)
        self.finalize_url = self.url("core_order_qc_finalize")

    def test_form_shows_previews_criteria_hints_and_two_decisions(self):
        content = self.client.get(self.finalize_url).content.decode()
        for code, (title, hint) in QC_CRITERIA.items():
            self.assertIn(title, content)
            self.assertIn(hint, content)
            self.assertIn(f'name="{code}" value="ok"', content)
            self.assertIn(f'name="{code}" value="defect"', content)
        self.assertIn("Норма", content)
        self.assertIn("Дефект", content)
        self.assertIn('value="pass"', content)
        self.assertIn('value="fail"', content)
        self.assertIn("QC пройден", content)
        self.assertIn("Отправить на доработку", content)
        for asset in QcService.current_final_assets(self.order):
            self.assertIn(f'<img src="{reverse("admin:core_preview_asset_file", args=[asset.pk])}"', content)
        self.assertIn("«Привет»", content)

    def assert_no_outcome(self):
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, QcReport.Status.IN_PROGRESS)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)

    def test_empty_submit_is_a_validation_error_not_a_fail(self):
        response = self.client.post(self.finalize_url, {"decision": "pass"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите «Норма» или «Дефект» для каждого критерия")
        self.assertContains(response, "Сходство лица")
        self.assert_no_outcome()

    def test_partial_answers_are_rejected_and_kept_in_the_form(self):
        data = {"likeness_face": "ok", "crop": "defect", "decision": "fail"}
        response = self.client.post(self.finalize_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Волосы и края")
        self.assertContains(response, 'name="likeness_face" value="ok" required checked')
        self.assertContains(response, 'name="crop" value="defect" checked')
        self.assert_no_outcome()

    def test_pass_with_a_defect_is_rejected(self):
        data = {c: "ok" for c in HUMAN_CRITERIA}
        data["ai_artifacts"] = "defect"
        data["decision"] = "pass"
        response = self.client.post(self.finalize_url, data)
        self.assertContains(response, "«QC пройден» невозможен: отмечены дефекты — Без артефактов.")
        self.assert_no_outcome()

    def test_fail_without_defects_is_rejected(self):
        data = {c: "ok" for c in HUMAN_CRITERIA}
        data["decision"] = "fail"
        response = self.client.post(self.finalize_url, data)
        self.assertContains(response, "требует хотя бы одного дефекта")
        self.assert_no_outcome()

    def test_missing_decision_is_rejected(self):
        response = self.client.post(self.finalize_url, {c: "ok" for c in HUMAN_CRITERIA})
        self.assertContains(response, "Выберите решение")
        self.assert_no_outcome()

    def test_all_ok_pass_reaches_ready_for_delivery(self):
        data = {**{c: "ok" for c in HUMAN_CRITERIA}, "decision": "pass"}
        response = self.client.post(self.finalize_url, data, follow=True)
        self.assertContains(response, "QC пройден — заказ готов к доставке.")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, QcReport.Status.PASSED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.READY_FOR_DELIVERY)

    def test_defect_fail_records_only_the_defects(self):
        data = {c: "ok" for c in HUMAN_CRITERIA}
        data["crop"] = "defect"
        data["decision"] = "fail"
        response = self.client.post(self.finalize_url, data, follow=True)
        self.assertContains(response, "QC не пройден: Кадрирование.")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, QcReport.Status.FAILED)
        self.assertEqual(self.report.reason_codes, ["crop"])
        self.assertContains(response, "Доработать этот слот")


class DeliveryPanelRuTests(ConsoleRuTestCase):
    def test_delivery_panel_and_messages_are_russian(self):
        self.produce()
        qc = QcService(storage=self.storage)
        qc.finalize_report(report=qc.start_qc(order=self.order), checklist={c: True for c in HUMAN_CRITERIA})
        self.order.refresh_from_db()
        adapter = FakeAdapter()
        adapter.channel = ChannelIdentity.Channel.MAX
        from apps.core.final_delivery_console import FinalDeliveryOrderAdmin
        from apps.core.services.final_delivery import FinalDeliveryService

        service = FinalDeliveryService(adapter=adapter, storage=self.storage)
        with patch.object(FinalDeliveryOrderAdmin, "get_final_delivery_service", return_value=service):
            response = self.client.post(self.url("core_order_deliver_final"), follow=True)
        content = response.content.decode()
        self.assertIn("Набор доставлен клиенту: «Привет»: отправлен, «Пока»: отправлен", content)
        self.assertIn("Доставка набора клиенту", content)
        self.assertIn("Слот «Привет» · отправлен · файл #", content)
        self.assertIn("Финальное сообщение: отправлен", content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DELIVERED)
