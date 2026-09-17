"""DRF-2054: negative gates of the Pilot chain (both channels, both products).

The matrix in tests_e2e_pilot_matrix proves the happy path; this module
proves that every stage refuses to advance without its precondition:

    G1  no generation before payment (webhook-level, both channels)
    G2  wrong amount / currency never confirms a payment (Stars, YooKassa)
    G3  no full production before customer approval
    G4  no full production without a confirmed payment
    G5  no QC / delivery before production is complete
    G6  no delivery before QC PASS; QC FAIL on non-sticker output blocks
        delivery (the 512 px / alpha risk recorded on DRF-2052)
    G7  channel isolation: a preview cannot leave through the other channel
    G8  the included revision is single-use

All gates are exercised on the real pilot catalog with the same fakes as
the matrix; nothing here reaches Telegram, MAX, YooKassa or OpenAI.
"""

from __future__ import annotations

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    Payment,
    QcReport,
    Revision,
)
from apps.core.services.full_production import FullProductionError, FullProductionService
from apps.core.services.generation import GenerationError, GenerationService
from apps.core.services.preview_delivery import PreviewDeliveryError, PreviewDeliveryService
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.core.services.qc import HUMAN_CRITERIA, QcError, QcService
from apps.core.tests_e2e_pilot_matrix import (
    ALL_EMOTIONS,
    PACK,
    SINGLE,
    FakeFinalDeliveryAdapter,
    FinalDeliveryService,
    MaxDriver,
    PilotE2ECase,
    TelegramDriver,
)
from apps.core.tests_qc import make_image
from apps.max_bot.preview_delivery import MaxPreviewDeliveryAdapter


class OpaqueLargeProvider:
    """What an unconfigured images.edit call is expected to return: 1024 px,
    no alpha. QC must FAIL such output and keep delivery closed."""

    name = "opaque-1024"

    def generate_preview(self, request):
        return ImageGenerationResult(content=make_image(size=(1024, 1024), mode="RGB"))


class PilotNegativeGateTests(PilotE2ECase):
    # -- G1 / G2: payment ------------------------------------------------

    def test_G1_no_preview_before_payment_on_both_channels(self):
        for driver, product in ((TelegramDriver(self), PACK), (MaxDriver(self), SINGLE)):
            order = driver.start_to_checkout(product, ALL_EMOTIONS if product == PACK else ["laugh"])
            self.assertIn(order.status, {Order.Status.READY_FOR_CHECKOUT, Order.Status.AWAITING_PAYMENT})
            with self.assertRaises(GenerationError):
                GenerationService(provider=self.provider, storage=self.storage).generate_preview(order=order)
            # console path refuses as well: no job, no status change
            self._console("core_order_generate_preview", order.pk)
            order.refresh_from_db()
            self.assertNotEqual(order.status, Order.Status.PAID)
            self.assertFalse(GenerationJob.objects.filter(order=order).exists())

    def test_G2_telegram_wrong_stars_amount_is_rejected(self):
        driver = TelegramDriver(self)
        order = driver.start_to_checkout(PACK, ALL_EMOTIONS)
        driver._post(driver._callback("pay"))
        invoice = driver.bot.send_invoice.call_args.kwargs
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.amount_minor, 460)

        for bad in (
            {"currency": "XTR", "total_amount": 100},  # single price on a pack order
            {"currency": "XTR", "total_amount": 500},  # RUB figure passed as Stars
            {"currency": "RUB", "total_amount": 460},
        ):
            pre_checkout = {
                "pre_checkout_query": {
                    "id": "pcq-bad",
                    "from": driver.user,
                    "invoice_payload": invoice["payload"],
                    **bad,
                }
            }
            self.assertEqual(driver._post(pre_checkout).status_code, 200)
            self.assertFalse(driver.bot.answer_pre_checkout_query.call_args.kwargs["ok"])
            successful = driver._message(
                successful_payment={
                    "invoice_payload": invoice["payload"],
                    "telegram_payment_charge_id": "tg-bad",
                    "provider_payment_charge_id": "p-bad",
                    **bad,
                }
            )
            response = driver._post(successful)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["ok"])
            order.refresh_from_db()
            payment.refresh_from_db()
            self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
            self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_G2_max_wrong_rub_amount_is_rejected_then_correct_confirms_once(self):
        driver = MaxDriver(self)
        order = driver.start_to_checkout(SINGLE, ["laugh"])
        response, payment = driver.pay(order, amount_minor=50000, external_id="yk-wrong")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(payment.status, Payment.Status.PENDING)

        response, payment = driver.pay(order)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order.status, Order.Status.PAID)
        # a replayed webhook is ACKed and stays a single confirmation
        response, _ = driver.pay(order)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.filter(order=order, status=Payment.Status.CONFIRMED).count(), 1)

    # -- G3 / G4: production entry ------------------------------------------

    def _paid_order_with_delivered_preview(self, driver, product):
        emotions = ALL_EMOTIONS if product == PACK else ["laugh"]
        order = driver.start_to_checkout(product, emotions)
        driver.pay(order)
        order.refresh_from_db()
        preview = self.operator_generate_preview(order)
        self.operator_approve_and_deliver(driver, order, preview)
        return order, preview

    def test_G3_no_full_production_before_customer_approval(self):
        order, _preview = self._paid_order_with_delivered_preview(TelegramDriver(self), SINGLE)
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self._console("core_order_start_full_production", order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertFalse(GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.FULL).exists())
        with self.assertRaisesMessage(FullProductionError, "no customer-approved preview"):
            FullProductionService(provider=self.provider, storage=self.storage).start(order=order, max_slots=None)

    def test_G4_no_full_production_without_confirmed_payment(self):
        order, preview = self._paid_order_with_delivered_preview(MaxDriver(self), PACK)
        PreviewFeedbackService.approve(order=order)
        # Simulate a confirmed payment that was reversed/lost: the gate must close.
        Payment.objects.filter(order=order).update(status=Payment.Status.PENDING)
        with self.assertRaisesMessage(FullProductionError, "no confirmed payment"):
            FullProductionService(provider=self.provider, storage=self.storage).start(order=order, max_slots=None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertFalse(GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.FULL).exists())

    # -- G5 / G6: QC and delivery --------------------------------------------

    def test_G5_no_qc_or_delivery_while_production_incomplete(self):
        driver = TelegramDriver(self)
        order, _preview = self._paid_order_with_delivered_preview(driver, PACK)
        driver.customer_approve()
        # 3 of 9 slots produced
        for _ in range(3):
            self._console("core_order_start_full_production", order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(len(QcService.current_final_assets(order)), 3)
        with self.assertRaises(QcError):
            QcService(storage=self.storage).start_qc(order=order)
        with self.assertRaises(QcError):
            QcService(storage=self.storage).assert_delivery_allowed(order=order)
        self._console("core_order_qc_start", order.pk)
        self.assertFalse(QcReport.objects.filter(order=order).exists())
        if FinalDeliveryService is not None:
            from apps.core.services.final_delivery import FinalDeliveryError

            with self.assertRaises(FinalDeliveryError):
                FinalDeliveryService(
                    adapter=FakeFinalDeliveryAdapter(driver.channel), storage=self.storage
                ).deliver(order=order, max_items=None)
            self.assertFalse(order.final_deliveries.exists())

    def test_G6_qc_fails_non_sticker_output_and_keeps_delivery_closed(self):
        """Known live risk (DRF-2052 comment): FULL output that is not
        512 px with alpha must be caught by automated QC, not shipped."""
        driver = MaxDriver(self)
        order, _preview = self._paid_order_with_delivered_preview(driver, SINGLE)
        driver.customer_approve()
        FullProductionService(provider=OpaqueLargeProvider(), storage=self.storage).start(order=order, max_slots=None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)

        self._console("core_order_qc_start", order.pk)
        report = QcReport.objects.get(order=order)
        checks = report.automated_checks["laugh"]
        self.assertFalse(checks["dimensions"])
        self.assertFalse(checks["alpha_channel"])
        self.assertTrue(checks["decodable"])
        self._console("core_order_qc_finalize", order.pk, data={c: "on" for c in HUMAN_CRITERIA})
        report.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(report.status, QcReport.Status.FAILED)
        self.assertEqual(report.reason_codes, ["bad_dimensions", "missing_alpha"])
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        with self.assertRaises(QcError):
            QcService(storage=self.storage).assert_delivery_allowed(order=order)

        # human FAIL on a technically valid set is equally blocking
        self._console("core_order_qc_retry", order.pk, "laugh")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        FullProductionService(provider=self.provider, storage=self.storage).regenerate_slots(
            order=order, slot_keys=["laugh"], max_slots=None
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self._console("core_order_qc_start", order.pk)
        second = QcReport.objects.get(order=order, attempt=2)
        self.assertTrue(all(second.automated_checks["laugh"][k] for k in second.automated_checks["laugh"] if k != "asset_id"))
        checklist = {c: "on" for c in HUMAN_CRITERIA}
        checklist.pop("likeness_face")
        self._console("core_order_qc_finalize", order.pk, data=checklist)
        second.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(second.status, QcReport.Status.FAILED)
        self.assertEqual(second.reason_codes, ["likeness_face"])
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        with self.assertRaises(QcError):
            QcService(storage=self.storage).assert_delivery_allowed(order=order)

    # -- G7 / G8: channel isolation and revision budget ------------------------

    def test_G7_preview_cannot_leave_through_the_other_channel(self):
        driver = TelegramDriver(self)
        order = driver.start_to_checkout(SINGLE, ["laugh"])
        driver.pay(order)
        order.refresh_from_db()
        preview = self.operator_generate_preview(order)
        self._console("core_order_approve_preview", order.pk, preview.pk)
        wrong_channel = MaxPreviewDeliveryAdapter(client=MaxDriver(self).bot)
        with self.assertRaises(PreviewDeliveryError):
            PreviewDeliveryService(adapter=wrong_channel, storage=self.storage).deliver(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        self.assertEqual(order.channel_identity.channel, ChannelIdentity.Channel.TELEGRAM)

    def test_G8_included_revision_is_single_use(self):
        driver = MaxDriver(self)
        order, preview = self._paid_order_with_delivered_preview(driver, SINGLE)
        driver.customer_revision(Revision.Category.HAIR)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REVISION_REQUESTED)
        revised = GenerationService(provider=self.provider, storage=self.storage).generate_revision(order=order)
        order.refresh_from_db()
        self.operator_approve_and_deliver(driver, order, revised)

        # second revision request via the webhook is refused (409), state intact
        response = driver._post(driver._callback(f"preview_revision:{Revision.Category.FACE}"))
        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertEqual(Revision.objects.filter(order=order).count(), 1)
        with self.assertRaises(PreviewFeedbackError):
            PreviewFeedbackService.request_revision(order=order, category=Revision.Category.FACE)
        # ...but approval still works and only the revised preview is approved
        driver.customer_approve()
        revised.refresh_from_db()
        preview.refresh_from_db()
        self.assertTrue(revised.metadata.get("customer_approved"))
        self.assertFalse(preview.metadata.get("customer_approved"))
        self.assertEqual(
            order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).count(), 2
        )
