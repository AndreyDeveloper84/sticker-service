"""Custom-phrase sticker packs in the Production Console.

Owner patch (bot menu + custom stickers) added the «white_outline» human
criterion and products with ``requires_custom_phrases`` /
``requires_customer_contact``. The console must render every service
criterion (the checklist was uncompletable when one was missing from the
console vocabulary), show the customer's contact and phrases on the order
card, name custom slots by their phrase, and let the operator filter by
product.
"""

import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.console_text import QC_CRITERIA, criterion_text, slot_title
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
    Style,
    User,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.full_production import FullProductionService
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_qc import make_image

PHRASES = ["Доброе утро", "Пошли пить кофе", "Я в пути"]
CUSTOM_CONFIG = {
    "kind": "custom_pack",
    "quantity": 3,
    "emotion_count": 3,
    "emotions": [{"code": f"custom-{n}", "label": f"Фраза {n}"} for n in range(1, 4)],
    "requires_custom_phrases": True,
    "requires_customer_contact": True,
    "price_minor": 80000,
}
PLAIN_CONFIG = {
    "kind": "pack",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": [{"code": "hello", "label": "Привет"}],
    "price_minor": 10000,
}


class StickerProvider:
    name = "fake"

    def generate_preview(self, request):
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata={})


class CustomConsoleTestCase(TestCase):
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
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="77", display_name="Анна", username="anna"
        )
        self.custom = Product.objects.create(code="sticker-pack-3-custom", name="3 стикера с надписями", config=CUSTOM_CONFIG)
        self.plain = Product.objects.create(code="single", name="Один стикер", config=PLAIN_CONFIG)
        style = Style.objects.create(code="comic", name="Комикс")
        self.order = Order.objects.create(
            user=user, channel_identity=self.identity, product=self.custom, style=style,
            status=Order.Status.PREVIEW_REVIEW,
            selection={
                "emotions": ["custom-1", "custom-2", "custom-3"],
                "custom_phrases": PHRASES,
                "contact": "+7 900 000-00-00, Анна",
            },
        )
        self.plain_order = Order.objects.create(
            user=user, channel_identity=self.identity, product=self.plain, style=style,
            status=Order.Status.AWAITING_PHOTOS, selection={"emotions": ["hello"]},
        )
        photo_key = f"orders/{self.order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(order=self.order, storage_key=photo_key, original_filename="p.jpg", mime_type="image/jpeg", size_bytes=5)
        Payment.objects.create(
            order=self.order, provider="telegram_stars", status=Payment.Status.CONFIRMED,
            amount_minor=736, currency="XTR", external_payment_id="c-1", confirmed_at=timezone.now(),
        )
        job = GenerationJob.objects.create(
            order=self.order, task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED, attempt=1, provider="fake",
        )
        key = f"generated/order-{self.order.pk}/preview/{job.pk}.png"
        self.storage.save(key, BytesIO(make_image()))
        GeneratedAsset.objects.create(
            order=self.order, job=job, kind=GeneratedAsset.Kind.PREVIEW, storage_key=key, size_bytes=10,
            metadata={"internal_approved": True, "customer_approved": True, "deliveries": [{"status": "sent", "channel": "telegram"}]},
        )
        provider, storage = StickerProvider(), self.storage
        patcher = patch.object(
            ProductionOrderAdmin, "get_full_production_service",
            lambda self_: FullProductionService(provider=provider, storage=storage),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def produce(self):
        FullProductionService(provider=StickerProvider(), storage=self.storage).start(order=self.order, max_slots=None)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.QUALITY_CONTROL)

    def change_page(self, order=None):
        response = self.client.get(reverse("admin:core_order_change", args=[(order or self.order).pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class CriteriaVocabularyTests(CustomConsoleTestCase):
    def test_every_service_criterion_has_console_text(self):
        for code in HUMAN_CRITERIA:
            title, hint = criterion_text(code)
            self.assertIn(code, QC_CRITERIA, code)
            self.assertNotEqual(title, code, code)
            self.assertTrue(hint, code)
        self.assertIn("white_outline", HUMAN_CRITERIA)
        self.assertEqual(criterion_text("white_outline")[0], "Белая обводка")
        # console order follows the service, «Белая обводка» right after «Прозрачный фон»
        titles = [criterion_text(code)[0] for code in HUMAN_CRITERIA]
        self.assertEqual(titles.index("Белая обводка"), titles.index("Прозрачный фон") + 1)

    def test_checklist_renders_all_seven_criteria_and_passes(self):
        self.produce()
        QcService(storage=self.storage).start_qc(order=self.order)
        url = reverse("admin:core_order_qc_finalize", args=[self.order.pk])
        content = self.client.get(url).content.decode()
        for code in HUMAN_CRITERIA:
            self.assertIn(f'name="{code}" value="ok"', content, code)
            self.assertIn(criterion_text(code)[0], content, code)
        self.assertIn("Аккуратная ровная белая обводка", content)
        self.assertIn(f"по {len(HUMAN_CRITERIA)} критериям", self.change_page())

        # six answers (the old set) are not enough: validation names the missing one
        six = {c: "ok" for c in HUMAN_CRITERIA if c != "white_outline"}
        response = self.client.post(url, {**six, "decision": "pass"})
        self.assertContains(response, "Выберите «Норма» или «Дефект» для каждого критерия: Белая обводка.")
        self.assertEqual(QcReport.objects.get(order=self.order).status, QcReport.Status.IN_PROGRESS)

        response = self.client.post(url, {**{c: "ok" for c in HUMAN_CRITERIA}, "decision": "pass"}, follow=True)
        self.assertContains(response, "QC пройден — заказ готов к доставке.")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.READY_FOR_DELIVERY)

    def test_white_outline_defect_fails_with_russian_reason(self):
        self.produce()
        QcService(storage=self.storage).start_qc(order=self.order)
        url = reverse("admin:core_order_qc_finalize", args=[self.order.pk])
        data = {c: "ok" for c in HUMAN_CRITERIA}
        data["white_outline"] = "defect"
        data["decision"] = "fail"
        response = self.client.post(url, data, follow=True)
        self.assertContains(response, "QC не пройден: Белая обводка.")
        report = QcReport.objects.get(order=self.order)
        self.assertEqual(report.status, QcReport.Status.FAILED)
        self.assertEqual(report.reason_codes, ["white_outline"])


class OrderCardTests(CustomConsoleTestCase):
    def test_card_shows_customer_contact_and_numbered_phrases(self):
        content = self.change_page()
        self.assertIn("Контакт для связи: <strong>+7 900 000-00-00, Анна</strong>", content)
        self.assertIn("Telegram · Анна · id 77 · @anna", content)
        self.assertIn("Фразы для стикеров", content)
        self.assertIn("<ol", content)
        for phrase in PHRASES:
            self.assertIn(f"<li>{phrase}</li>", content)

    def test_card_without_phrases_or_contact_says_so(self):
        self.order.selection = {"emotions": ["custom-1", "custom-2", "custom-3"]}
        self.order.save(update_fields=["selection"])
        content = self.change_page()
        self.assertIn("Контакт для связи: ещё не указан", content)
        self.assertIn("Клиент ещё не прислал фразы.", content)
        # a plain product shows neither prompt
        content = self.change_page(self.plain_order)
        self.assertNotIn("Клиент ещё не прислал фразы.", content)
        self.assertNotIn("Контакт для связи", content)

    def test_production_panel_and_next_step_name_custom_slots_by_phrase(self):
        self.assertEqual(slot_title(self.order, "custom-2"), "«Пошли пить кофе»")
        self.assertEqual(slot_title(self.plain_order, "hello"), "«Привет»")
        long_order = self.order
        long_order.selection = {**long_order.selection, "custom_phrases": ["x" * 60, "b", "c"]}
        self.assertEqual(len(slot_title(long_order, "custom-1")), 42)  # «…» truncated to 40
        long_order.refresh_from_db()

        self.produce()
        content = self.change_page()
        for phrase in PHRASES:
            self.assertIn(f"Слот «{phrase}» · готов", content)
        self.assertNotIn("«Фраза 1»", content)
        # QC retry: next step names the phrase too
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=self.order)
        checklist = {c: True for c in HUMAN_CRITERIA}
        checklist["crop"] = False
        qc.request_retry(report=qc.finalize_report(report=report, checklist=checklist), slot_keys=["custom-3"])
        content = self.change_page()
        self.assertIn("Перегенерировать слот «Я в пути»", content)


class OrderListTests(CustomConsoleTestCase):
    def test_list_shows_product_names_and_filters_by_product(self):
        response = self.client.get(reverse("admin:core_order_changelist"))
        content = response.content.decode()
        self.assertIn("3 стикера с надписями", content)
        self.assertIn("Один стикер", content)
        self.assertIn("продукт", content)  # filter title
        response = self.client.get(reverse("admin:core_order_changelist"), {"product": self.custom.pk})
        content = response.content.decode()
        self.assertIn(f">#{self.order.pk}<", content)
        self.assertNotIn(f">#{self.plain_order.pk}<", content)
