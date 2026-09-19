"""DRF-2111 PR-B — order unit economics + console block.

Five synthetic cases (A–E) the orchestrator re-checks by hand, plus the
XTR / revision / rate-snapshot / query-count guarantees. All AI costs come
from real service runs with a fake provider under a 7.42 ₽ tariff; fees are
provider-confirmed evidence on the payment; manual minutes carry the rate
snapshot taken at logging time.
"""

import re
from contextlib import suppress
from io import BytesIO
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core.models import (
    GeneratedAsset, GenerationJob, ManualWorkLog, Order, OrderEvent, OrderPhoto, Payment, Product, Revision,
)
from apps.core.services import generation_cost as gc
from apps.core.services.order_economics import (
    NOT_COMPUTABLE, PROVIDER_CONFIRMED, RATE_KEY, RATE_SOURCE_CONFIG_SNAPSHOT, RATE_SOURCE_NOT_CONFIGURED,
    UNKNOWN, OrderEconomics, manual_work_snapshot, payment_fee,
)
from apps.core.services.full_production import FullProductionError
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.tests_generation_cost import CostFixture
from apps.core.tests_pilot_budget import UNLIMITED

TARIFF = 742  # 7.42 ₽ per call, minor units
RATE_PER_HOUR = 60000  # 600 ₽/h
PRICED = dict(
    UNLIMITED,
    PILOT_IMAGE_CALL_COST_RUB=7.42, PILOT_IMAGE_PRICING_VERSION="pilot-tariff-1",
    PILOT_OPERATOR_COST_PER_HOUR_RUB=600.0, PILOT_OPERATOR_RATE_VERSION="op-rate-1",
)
EMOTION_CODES = ["hello", "bye", "thanks", "great", "no", "love", "laugh", "angry", "surprised"]


def pack_config(n, price_minor):
    return {
        "kind": "pack", "quantity": n, "emotion_count": n,
        "emotions": [{"code": code, "label": code.title()} for code in EMOTION_CODES[:n]],
        "price_minor": price_minor,
    }


def custom_config(n, price_minor):
    return {
        "kind": "custom_pack", "quantity": n, "emotion_count": n,
        "emotions": [{"code": f"custom-{i}", "label": f"Фраза {i}"} for i in range(1, n + 1)],
        "requires_custom_phrases": True, "price_minor": price_minor,
    }


PHRASES = ["Доброе утро", "Пошли пить кофе", "Я в пути", "Спасибо!", "Не сегодня",
           "Люблю", "Ха-ха", "Ну всё", "Ого"]


def fee_metadata(amount_minor, fee_minor):
    return {"fee": {
        "amount_minor": fee_minor, "income_amount_minor": amount_minor - fee_minor, "currency": "RUB",
        "source": PROVIDER_CONFIRMED, "captured_at": timezone.now().isoformat(),
    }}


@override_settings(**PRICED)
class EconomicsFixture(CostFixture):
    def _order(self, config, *, amount_minor, currency="RUB", provider="yookassa", fee_minor=None,
               custom_phrases=None, code="pack-eco"):
        product = Product.objects.create(code=f"{code}-{Product.objects.count()}", name=code, config=config)
        selection = {"emotions": [e["code"] for e in config["emotions"]]}
        if custom_phrases:
            selection["custom_phrases"] = custom_phrases
        order = Order.objects.create(
            user=self.identity.user, channel_identity=self.identity, product=product, style=self.order.style,
            status=Order.Status.PAID, selection=selection,
        )
        photo_key = f"orders/{order.pk}/source/photo.jpg"
        self.storage.save(photo_key, BytesIO(b"photo"))
        OrderPhoto.objects.create(order=order, storage_key=photo_key, original_filename="photo.jpg",
                                  mime_type="image/jpeg", size_bytes=5)
        metadata = fee_metadata(amount_minor, fee_minor) if fee_minor is not None else {}
        Payment.objects.create(order=order, provider=provider, status=Payment.Status.CONFIRMED,
                               amount_minor=amount_minor, currency=currency, metadata=metadata,
                               external_payment_id=f"pay-{order.pk}", confirmed_at=timezone.now())
        return order

    def _preview(self, order):
        """Real priced preview, then customer approval → PREVIEW_REVIEW."""
        asset = self.generation.generate_preview(order=order)
        asset.metadata = {**(asset.metadata or {}), "internal_approved": True, "customer_approved": True}
        asset.save(update_fields=["metadata"])
        order.refresh_from_db()
        order.status = Order.Status.PREVIEW_REVIEW  # internal review + send to customer, done
        order.save(update_fields=["status"])
        return asset

    def _log_minutes(self, order, minutes):
        return OrderEvent.objects.create(
            order=order, event_type=OrderEvent.Type.MANUAL_WORK_LOGGED, actor_kind=OrderEvent.Actor.OPERATOR,
            payload={"minutes": minutes, "activity": "qc", "note": "", RATE_KEY: manual_work_snapshot(minutes)},
        )

    def _card(self, order):
        response = self.client.get(reverse("admin:core_order_change", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def _block(self, response):
        html = response.content.decode()
        start = html.index("Выручка:")
        return html[start: html.index("</div>", start)]


class SyntheticCasesTests(EconomicsFixture):
    def test_case_a_single_sticker_100_rub(self):
        """A) 1×100 ₽: preview + 1 FULL from the first attempt; fee 3,50 ₽; 5 min."""
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        self._log_minutes(order, 5)

        eco = OrderEconomics.compute(order)

        self.assertEqual(eco["revenue"], {"amount_minor": 10000, "currency": "RUB"})
        self.assertEqual(eco["payment_fee"]["amount_minor"], 350)
        self.assertEqual(eco["ai"]["preview"]["known_cost_minor"], TARIFF)
        self.assertEqual(eco["ai"]["full"]["known_cost_minor"], TARIFF)
        self.assertEqual(eco["ai"]["full"]["calls"], 1)
        self.assertEqual(eco["ai"]["regeneration"]["calls"], 0)
        self.assertEqual(eco["ai"]["total"]["known_cost_minor"], 2 * TARIFF)  # 14,84 ₽
        self.assertEqual(eco["manual"], {"minutes": 5, "entries": 1, "cost_minor": 5000,
                                         "rate_source": RATE_SOURCE_CONFIG_SNAPSHOT})
        self.assertEqual(eco["known_variable_cost_minor"], 1484 + 350 + 5000)  # 68,34 ₽
        self.assertEqual(eco["known_contribution_minor"], 10000 - 6834)  # 31,66 ₽
        self.assertEqual(eco["unknown_components"], [])

        block = self._block(self._card(order))
        self.assertIn("Выручка: 100,00 ₽ (yookassa)", block)
        self.assertIn("превью: 1 вызов(ов), 7,42 ₽", block)
        self.assertIn("производство: 1 вызов(ов), 7,42 ₽", block)
        self.assertIn("перегенерации: нет вызовов", block)
        self.assertIn("AI всего: 14,84 ₽", block)
        self.assertIn("Ручная работа: 5 мин — 50,00 ₽", block)
        self.assertIn("Комиссия платежа: 3,50 ₽ (по данным провайдера)", block)
        self.assertIn("Известные переменные расходы: 68,34 ₽", block)
        self.assertIn("Известный contribution: 31,66 ₽", block)
        self.assertNotIn("не учтено", block)
        self.assertNotIn("прибыль", block.lower())

    def test_case_b_pack_9_at_500_rub(self):
        """B) 9×500 ₽ = 4500 ₽: preview + 9 FULL; fee 157,50 ₽; 30 min."""
        order = self._order(pack_config(9, 450000), amount_minor=450000, fee_minor=15750)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        self._log_minutes(order, 30)

        eco = OrderEconomics.compute(order)

        self.assertEqual(eco["ai"]["full"]["calls"], 9)
        self.assertEqual(eco["ai"]["full"]["known_cost_minor"], 9 * TARIFF)  # 66,78 ₽
        self.assertEqual([row["slot_key"] for row in eco["ai"]["full"]["slots"]], sorted(EMOTION_CODES))
        self.assertTrue(all(row["cost_minor"] == TARIFF for row in eco["ai"]["full"]["slots"]))
        self.assertEqual(eco["ai"]["total"]["known_cost_minor"], 10 * TARIFF)  # 74,20 ₽
        self.assertEqual(eco["manual"]["cost_minor"], 30000)
        self.assertEqual(eco["known_variable_cost_minor"], 7420 + 15750 + 30000)  # 531,70 ₽
        self.assertEqual(eco["known_contribution_minor"], 450000 - 53170)  # 3968,30 ₽

        block = self._block(self._card(order))
        self.assertIn("Выручка: 4500,00 ₽", block)
        self.assertIn("производство: 9 вызов(ов), 66,78 ₽", block)
        self.assertIn("AI всего: 74,20 ₽", block)
        self.assertIn("Слоты: «Angry» #", block)
        self.assertIn("Известные переменные расходы: 531,70 ₽", block)
        self.assertIn("Известный contribution: 3968,30 ₽", block)

    def test_case_c_custom_pack_9_at_800_rub_slot_titles_are_phrases(self):
        """C) 9×800 ₽ = 7200 ₽ with captions: slot titles are the customer's phrases."""
        order = self._order(custom_config(9, 720000), amount_minor=720000, fee_minor=25200,
                            custom_phrases=PHRASES, code="custom-eco")
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        self._log_minutes(order, 30)

        eco = OrderEconomics.compute(order)

        titles = [row["title"] for row in eco["ai"]["full"]["slots"]]
        self.assertEqual(sorted(titles), sorted(f"«{p}»" for p in PHRASES))
        self.assertEqual(eco["ai"]["total"]["known_cost_minor"], 10 * TARIFF)
        self.assertEqual(eco["known_variable_cost_minor"], 7420 + 25200 + 30000)  # 626,20 ₽
        self.assertEqual(eco["known_contribution_minor"], 720000 - 62620)  # 6573,80 ₽

        block = self._block(self._card(order))
        self.assertIn("Выручка: 7200,00 ₽", block)
        self.assertIn("«Пошли пить кофе» #", block)
        self.assertNotIn("Фраза 2", block)
        self.assertIn("Известные переменные расходы: 626,20 ₽", block)
        self.assertIn("Известный contribution: 6573,80 ₽", block)

    def test_case_d_regeneration_is_a_separate_line(self):
        """D) pack-3 with a QC regenerate + a force_retry: «Перегенерации» apart from FULL."""
        order = self._order(pack_config(3, 150000), amount_minor=150000, fee_minor=5250)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=order)
        checklist = {criterion: True for criterion in HUMAN_CRITERIA}
        checklist[next(iter(HUMAN_CRITERIA))] = False
        qc.finalize_report(report=report, checklist=checklist)
        report.refresh_from_db()
        qc.request_retry(report=report, slot_keys=["bye"])
        self.provider.fail = "ambiguous"
        with suppress(FullProductionError):
            self.production.regenerate_slots(order=order, slot_keys=["bye"])  # ambiguous → blocked
        self.provider.fail = None
        self.production.force_retry_slot(order=order, slot_key="bye")

        eco = OrderEconomics.compute(order)

        self.assertEqual(eco["ai"]["full"]["calls"], 3)
        self.assertEqual(eco["ai"]["full"]["known_cost_minor"], 3 * TARIFF)
        regen = eco["ai"]["regeneration"]
        self.assertEqual(regen["calls"], 2)
        self.assertEqual([row["mode"] for row in regen["slots"]], [gc.MODE_REGENERATE, gc.MODE_FORCE_RETRY])
        self.assertEqual(regen["known_cost_minor"], TARIFF)  # the force_retry succeeded
        self.assertEqual(regen["possibly_billable_count"], 1)  # the ambiguous one
        self.assertEqual(eco["ai"]["total"]["known_cost_minor"], 5 * TARIFF)  # 37,10 ₽
        self.assertEqual(eco["unknown_components"], ["ai_possibly_billable:1"])

        block = self._block(self._card(order))
        self.assertIn("производство: 3 вызов(ов), 22,26 ₽", block)
        self.assertIn("перегенерации: 2 вызов(ов), 7,42 ₽ (известно по 1 вызовам), возможно платных: 1", block)
        self.assertIn("Перегенерации: «Bye» #", block)
        self.assertIn("— неизвестна", block)
        self.assertIn("+ не учтено: возможно платных AI-вызовов: 1", block)

    def test_case_e_unknown_components_are_never_shown_as_zero(self):
        """E) historical job without snapshot + payment without fee + no operator rate."""
        order = self._order(pack_config(1, 10000), amount_minor=10000)  # no fee evidence
        GenerationJob.objects.create(  # historical preview: no cost snapshot
            order=order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.SUCCEEDED,
            attempt=1, provider="fake", started_at=timezone.now(), finished_at=timezone.now(),
        )
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_COST_PER_HOUR_RUB": ""}):
            self._log_minutes(order, 15)

        eco = OrderEconomics.compute(order)

        self.assertEqual(eco["payment_fee"]["source"], UNKNOWN)
        self.assertEqual(eco["ai"]["total"]["known_count"], 0)
        self.assertEqual(eco["manual"], {"minutes": 15, "entries": 1, "cost_minor": None,
                                         "rate_source": RATE_SOURCE_NOT_CONFIGURED})
        self.assertEqual(eco["known_variable_cost_minor"], 0)
        self.assertEqual(eco["known_contribution_minor"], 10000)
        self.assertEqual(
            eco["unknown_components"],
            ["payment_fee_unknown", "ai_unknown_price:1", "ai_possibly_billable:1", "manual_rate_not_configured"],
        )

        block = self._block(self._card(order))
        self.assertIn("превью: 1 вызов(ов), неизвестна, возможно платных: 1", block)
        self.assertIn("AI всего: неизвестна", block)
        self.assertIn("Ручная работа: 15 мин — не настроено", block)
        self.assertIn("Комиссия платежа: неизвестна", block)
        self.assertIn("Известные переменные расходы: нет известных", block)
        self.assertIn("Известный contribution: 100,00 ₽", block)
        self.assertIn("+ не учтено: комиссия платежа неизвестна; AI-вызовов без цены: 1; "
                      "возможно платных AI-вызовов: 1; ставка оператора не настроена", block)
        self.assertIsNone(re.search(r"(?<![\d,])0(,00)? ₽", block), block)


class EconomicsRulesTests(EconomicsFixture):
    def test_telegram_stars_revenue_is_not_computable(self):
        """XTR revenue is shown in XTR; costs in ₽ apart; no conversion."""
        self.production.start(order=self.order, max_slots=None)  # fixture: telegram_stars, 460 XTR
        eco = OrderEconomics.compute(self.order)
        self.assertEqual(eco["revenue"], {"amount_minor": 460, "currency": "XTR"})
        self.assertEqual(eco["payment_fee"]["source"], UNKNOWN)
        self.assertEqual(eco["known_contribution_minor"], NOT_COMPUTABLE)
        self.assertEqual(eco["ai"]["full"]["known_cost_minor"], 3 * TARIFF)
        self.assertEqual(eco["known_variable_cost_minor"], 3 * TARIFF)
        self.assertIn("revenue_currency_xtr", eco["unknown_components"])
        block = self._block(self._card(self.order))
        self.assertIn("Выручка: 460 XTR (telegram_stars)", block)
        self.assertIn("Contribution: не вычисляется (валюта XTR)", block)
        self.assertIn("выручка в XTR, конверсии нет", block)

    def test_revision_cost_lands_in_revision_stage(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        preview = self._preview(order)
        Revision.objects.create(order=order, source_preview=preview, category=Revision.Category.FACE)
        order.status = Order.Status.REVISION_REQUESTED
        order.save(update_fields=["status"])
        self.generation.generate_revision(order=order)
        eco = OrderEconomics.compute(order)
        self.assertEqual(eco["ai"]["revision"], {"calls": 1, "known_cost_minor": TARIFF, "known_count": 1,
                                                  "possibly_billable_count": 0, "unknown_price_count": 0,
                                                  "not_billable_count": 0})
        self.assertIn("правки: 1 вызов(ов), 7,42 ₽", self._block(self._card(order)))

    def test_no_confirmed_payment(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000)
        Payment.objects.filter(order=order).update(status=Payment.Status.PENDING)
        eco = OrderEconomics.compute(order)
        self.assertIsNone(eco["revenue"])
        self.assertEqual(eco["known_contribution_minor"], NOT_COMPUTABLE)
        self.assertIn("no_confirmed_payment", eco["unknown_components"])
        block = self._block(self._card(order))
        self.assertIn("Выручка: нет подтверждённого платежа", block)
        self.assertIn("Contribution: не вычисляется (нет выручки)", block)

    def test_all_calls_rejected_before_provider_is_zero_with_reason(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self.provider.fail = "transport"
        with self.assertRaises(Exception):
            self.generation.generate_preview(order=order)
        eco = OrderEconomics.compute(order)
        self.assertEqual(eco["ai"]["preview"]["not_billable_count"], 1)
        self.assertEqual(gc.format_known_cost(eco["ai"]["preview"]), "0 ₽ (провайдер не принял 1)")
        self.assertEqual(eco["unknown_components"], [])

    def test_payment_fee_requires_provider_confirmed_evidence(self):
        payment = Payment(metadata={"fee": {"amount_minor": 350, "currency": "RUB", "source": "ESTIMATED"}})
        self.assertEqual(payment_fee(payment)["source"], UNKNOWN)
        payment = Payment(metadata={})
        self.assertEqual(payment_fee(payment)["source"], UNKNOWN)
        self.assertEqual(payment_fee(None)["source"], UNKNOWN)


class OperatorRateSnapshotTests(EconomicsFixture):
    def test_snapshot_fields(self):
        snapshot = manual_work_snapshot(30)
        self.assertEqual(snapshot["rate_minor_per_hour"], RATE_PER_HOUR)
        self.assertEqual(snapshot["rate_version"], "op-rate-1")
        self.assertEqual(snapshot["cost_minor"], 30000)
        self.assertEqual(snapshot["currency"], "RUB")
        self.assertEqual(snapshot["rate_source"], RATE_SOURCE_CONFIG_SNAPSHOT)

    def test_no_rate_means_no_snapshot(self):
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_COST_PER_HOUR_RUB": ""}):
            self.assertIsNone(manual_work_snapshot(30))

    def test_default_rate_version_is_rate_at_date(self):
        with override_settings(PILOT_OPERATOR_RATE_VERSION=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_RATE_VERSION": ""}):
            self.assertEqual(manual_work_snapshot(10)["rate_version"], f"600@{timezone.localdate().isoformat()}")

    def test_console_form_writes_snapshot(self):
        response = self.client.post(
            reverse("admin:core_manualworklog_add"),
            data={"order": self.order.pk, "minutes": 20, "activity": "qc", "note": ""}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        log = ManualWorkLog.objects.get(order=self.order)
        self.assertEqual(log.payload["minutes"], 20)
        self.assertEqual(log.payload[RATE_KEY]["cost_minor"], 20000)
        self.assertEqual(log.payload[RATE_KEY]["rate_version"], "op-rate-1")

    def test_console_form_without_rate_writes_no_snapshot(self):
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_COST_PER_HOUR_RUB": ""}):
            self.client.post(
                reverse("admin:core_manualworklog_add"),
                data={"order": self.order.pk, "minutes": 20, "activity": "qc", "note": ""}, follow=True,
            )
        log = ManualWorkLog.objects.get(order=self.order)
        self.assertNotIn(RATE_KEY, log.payload)
        self.assertEqual(OrderEconomics.compute(self.order)["manual"]["rate_source"], RATE_SOURCE_NOT_CONFIGURED)

    def test_rate_change_tomorrow_does_not_reprice_old_logs(self):
        """Same guarantee as PR-A's price snapshot, for the operator rate."""
        self._log_minutes(self.order, 30)
        before = OrderEconomics.compute(self.order)["manual"]
        self.assertEqual(before["cost_minor"], 30000)
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=1200.0, PILOT_OPERATOR_RATE_VERSION="op-rate-2"):
            self.assertEqual(OrderEconomics.compute(self.order)["manual"], before)
            self._log_minutes(self.order, 30)  # a NEW log gets the new rate
            manual = OrderEconomics.compute(self.order)["manual"]
            self.assertEqual(manual["minutes"], 60)
            self.assertEqual(manual["cost_minor"], 30000 + 60000)
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_COST_PER_HOUR_RUB": ""}):
            self.assertEqual(OrderEconomics.compute(self.order)["manual"]["cost_minor"], 90000)
        # a log written while the rate was unset keeps the order "не настроено" even after
        # the rate is configured again
        OrderEvent.objects.create(order=self.order, event_type=OrderEvent.Type.MANUAL_WORK_LOGGED,
                                  payload={"minutes": 5, "activity": "qc"})
        self.assertIsNone(OrderEconomics.compute(self.order)["manual"]["cost_minor"])


class QueryCountTests(EconomicsFixture):
    def _big_order(self):
        order = self._order(pack_config(9, 450000), amount_minor=450000, fee_minor=15750)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=order)
        checklist = {criterion: True for criterion in HUMAN_CRITERIA}
        checklist[next(iter(HUMAN_CRITERIA))] = False
        qc.finalize_report(report=report, checklist=checklist)
        report.refresh_from_db()
        qc.request_retry(report=report, slot_keys=["hello", "bye", "thanks"])
        self.production.regenerate_slots(order=order, slot_keys=["hello", "bye", "thanks"], max_slots=None)
        self._log_minutes(order, 30)
        return order

    def test_compute_uses_three_queries_on_a_bare_order_and_none_when_prefetched(self):
        order = self._big_order()
        self.assertEqual(GenerationJob.objects.filter(order=order, task_type="full").count(), 12)
        bare = Order.objects.select_related("product", "channel_identity").get(pk=order.pk)
        with self.assertNumQueries(3):  # generation_jobs, payments, events
            eco = OrderEconomics.compute(bare)
        self.assertEqual(eco["ai"]["full"]["calls"], 9)
        self.assertEqual(eco["ai"]["regeneration"]["calls"], 3)
        prefetched = (
            Order.objects.select_related("product", "channel_identity")
            .prefetch_related("generation_jobs", "payments", "events").get(pk=order.pk)
        )
        with self.assertNumQueries(0):
            OrderEconomics.compute(prefetched)
        self.assertEqual(self.provider.calls, 1 + 9 + 3)  # analytics made no provider call

    def test_order_card_query_count_does_not_grow_with_jobs(self):
        small = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self._preview(small)
        self.production.start(order=small, max_slots=None)
        self._log_minutes(small, 5)
        big = self._big_order()
        url = reverse("admin:core_order_change", args=[small.pk])
        with CaptureQueriesContext(connection) as small_queries:
            self.client.get(url)
        with CaptureQueriesContext(connection) as big_queries:
            self.client.get(reverse("admin:core_order_change", args=[big.pk]))
        self.assertEqual(len(big_queries), len(small_queries))
