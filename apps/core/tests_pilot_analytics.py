"""DRF-2111 PR-C — «Метрики Pilot»: service, page, export.

Covers: period boundaries in Europe/Moscow (23:59:59 vs 00:00:00, month
end → 1st), RUB / XTR kept apart, AI cost by stage, quality, operations,
unit economics per product from the DB (three-product regression), budget
flags 79 / 80 / 100 %, CSV / JSON export per §21 (UNKNOWN → empty, no PII)
and a fixed query count (3 vs 30 orders).
"""

import csv
import io
import json
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import GenerationJob, Order, OrderEvent, Product, QcReport, Revision
from apps.core.services import generation_cost as gc
from apps.core.services.pilot_analytics import EXPORT_COLUMNS, PilotAnalyticsService, period_for
from apps.core.tests_order_economics import EconomicsFixture, PHRASES, TARIFF, custom_config, pack_config

MSK = ZoneInfo("Europe/Moscow")


def msk(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=MSK)


def set_created(obj, moment):
    type(obj).objects.filter(pk=obj.pk).update(created_at=moment)
    obj.refresh_from_db()


class PeriodTests(EconomicsFixture):
    def test_presets_are_local_midnight_half_open(self):
        now = msk(2026, 9, 19, 15, 30)
        today = period_for("today", now=now)
        self.assertEqual(today.start, msk(2026, 9, 19))
        self.assertEqual(today.end, msk(2026, 9, 20))
        week = period_for("7d", now=now)
        self.assertEqual(week.start, msk(2026, 9, 13))
        self.assertEqual(week.end, msk(2026, 9, 20))
        month = period_for("30d", now=now)
        self.assertEqual(month.start, msk(2026, 8, 21))
        self.assertEqual(period_for("bogus", now=now), today)

    def test_custom_period_is_inclusive_calendar_days_and_swaps_inverted(self):
        period = period_for("custom", start=date(2026, 9, 30), end=date(2026, 9, 1))
        self.assertEqual(period.start, msk(2026, 9, 1))
        self.assertEqual(period.end, msk(2026, 10, 1))
        self.assertEqual(period.end_date, date(2026, 9, 30))
        self.assertEqual(period.label, "2026-09-01_2026-09-30")

    def test_day_boundaries_2359_in_0000_out(self):
        period = period_for("custom", start=date(2026, 9, 18), end=date(2026, 9, 18))
        inside = self._order(pack_config(1, 10000), amount_minor=10000)
        outside_after = self._order(pack_config(1, 10000), amount_minor=10000)
        outside_before = self._order(pack_config(1, 10000), amount_minor=10000)
        set_created(inside, msk(2026, 9, 18, 23, 59, 59))
        set_created(outside_after, msk(2026, 9, 19, 0, 0, 0))
        set_created(outside_before, msk(2026, 9, 17, 23, 59, 59))
        ids = [o.pk for o in PilotAnalyticsService(period).orders()]
        self.assertEqual(ids, [inside.pk])

    def test_month_end_boundary_31st_vs_1st(self):
        period = period_for("custom", start=date(2026, 8, 1), end=date(2026, 8, 31))
        last_second = self._order(pack_config(1, 10000), amount_minor=10000)
        first_second = self._order(pack_config(1, 10000), amount_minor=10000)
        set_created(last_second, msk(2026, 8, 31, 23, 59, 59))
        set_created(first_second, msk(2026, 9, 1, 0, 0, 0))
        ids = [o.pk for o in PilotAnalyticsService(period).orders()]
        self.assertEqual(ids, [last_second.pk])
        # the UTC instant of MSK midnight is 21:00 the previous day — the
        # boundary must be the local one, not the UTC one
        self.assertEqual(period.end.astimezone(ZoneInfo("UTC")).hour, 21)

    def test_orders_block_uses_events_in_period_not_creation_date(self):
        old = self._order(pack_config(1, 10000), amount_minor=10000)
        set_created(old, msk(2026, 9, 1))
        OrderEvent.objects.create(order=old, event_type=OrderEvent.Type.STATUS_CHANGED,
                                  from_status="ready_for_delivery", to_status=Order.Status.DELIVERED)
        OrderEvent.objects.create(order=old, event_type=OrderEvent.Type.STATUS_CHANGED,
                                  from_status="x", to_status=Order.Status.FAILED)
        period = period_for("today")
        block = PilotAnalyticsService(period).snapshot()["orders"]
        self.assertEqual(block["delivered"], 1)
        self.assertEqual(block["failed"], 1)
        self.assertEqual(block["started"], Order.objects.filter(created_at__gte=period.start).count())


class ThreeProductsFixture(EconomicsFixture):
    """Three products (single / pack-9 / pack-9-custom) + the fixture's XTR order."""

    def setUp(self):
        super().setUp()
        self.single = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350, code="single-sticker")
        self._preview(self.single)
        self.production.start(order=self.single, max_slots=None)
        self._log_minutes(self.single, 5)
        OrderEvent.objects.create(order=self.single, event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED)
        OrderEvent.objects.create(order=self.single, event_type=OrderEvent.Type.STATUS_CHANGED,
                                  from_status="ready_for_delivery", to_status=Order.Status.DELIVERED)

        self.pack9 = self._order(pack_config(9, 450000), amount_minor=450000, fee_minor=15750, code="sticker-pack-9")
        preview = self._preview(self.pack9)
        Revision.objects.create(order=self.pack9, source_preview=preview, category=Revision.Category.FACE)
        OrderEvent.objects.create(order=self.pack9, event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED)
        self.production.start(order=self.pack9, max_slots=None)

        self.custom9 = self._order(custom_config(9, 720000), amount_minor=720000, custom_phrases=PHRASES,
                                   code="sticker-pack-9-custom")
        self._preview(self.custom9)
        with override_settings(PILOT_OPERATOR_COST_PER_HOUR_RUB=None), \
                patch.dict("os.environ", {"PILOT_OPERATOR_COST_PER_HOUR_RUB": ""}):
            self._log_minutes(self.custom9, 20)
        # fixture self.order: telegram_stars 460 XTR, historical preview, no logs
        self.snapshot = PilotAnalyticsService(period_for("today")).snapshot()


class SnapshotTests(ThreeProductsFixture):
    def test_funnel_counts_and_conversions(self):
        steps = {s["step"]: s for s in self.snapshot["funnel"]["steps"]}
        self.assertEqual(steps["start"]["count"], 4)
        self.assertEqual(steps["payment"]["count"], 4)
        self.assertEqual(steps["preview"]["count"], 4)  # incl. the fixture's historical (succeeded) preview
        self.assertEqual(steps["approval"]["count"], 2)
        self.assertEqual(steps["delivered"]["count"], 1)
        self.assertEqual(steps["payment"]["conversion_percent"], 100.0)
        self.assertEqual(steps["delivered"]["conversion_percent"], 50.0)
        self.assertEqual(self.snapshot["funnel"]["overall_percent"], 25.0)

    def test_revenue_is_split_per_currency(self):
        revenue = self.snapshot["revenue"]
        totals = {t["currency"]: t for t in revenue["totals"]}
        self.assertEqual(totals["RUB"]["amount_minor"], 10000 + 450000 + 720000)
        self.assertEqual(totals["RUB"]["orders"], 3)
        self.assertEqual(totals["XTR"]["amount_minor"], 460)
        cells = {(c["product"], c["channel"], c["currency"]): c["amount_minor"] for c in revenue["cells"]}
        self.assertEqual(cells[("pack3", "telegram", "XTR")], 460)
        self.assertEqual(cells[(self.single.product.code, "telegram", "RUB")], 10000)
        for cell in revenue["cells"]:
            self.assertIn(cell["currency"], ("RUB", "XTR"))

    def test_ai_cost_by_stage_and_per_paid_order(self):
        ai = self.snapshot["ai_cost"]
        self.assertEqual(ai["stages"]["preview"]["known_cost_minor"], 3 * TARIFF)
        self.assertEqual(ai["stages"]["preview"]["unknown_price_count"], 1)  # fixture's historical preview
        self.assertEqual(ai["stages"]["full"]["known_cost_minor"], 10 * TARIFF)
        self.assertEqual(ai["stages"]["regeneration"]["calls"], 0)
        self.assertEqual(ai["total"]["known_cost_minor"], 13 * TARIFF)
        self.assertEqual(ai["total"]["unknown_price_count"], 1)
        self.assertEqual(ai["paid_orders"], 4)
        self.assertEqual(ai["paid_orders_fully_known"], 3)
        # single: 2 calls, pack9: 10 calls, custom9: 1 call → mean 13/3 calls
        self.assertEqual(ai["known_cost_minor_per_paid_order_avg"], round(13 * TARIFF / 3, 2))
        self.assertEqual(ai["known_cost_minor_per_paid_order_median"], 2 * TARIFF)

    def test_quality_block(self):
        quality = self.snapshot["quality"]
        self.assertEqual(quality["orders_with_preview"], 4)
        self.assertEqual(quality["preview_first_try_accepted"], 1)  # single approved without revision
        self.assertEqual(quality["orders_with_revision"], 1)
        self.assertEqual(quality["moderation_failures"], 0)
        self.assertEqual(quality["qc_retries"], 0)

    def test_quality_counts_moderation_failures_and_qc_retries(self):
        self.provider.fail = "moderation"
        with patch("apps.core.services.generation.describe_provider_failure",
                   return_value={"failure_class": "moderation", "moderation_stage": "input"}):
            with self.assertRaises(Exception):
                self.generation.generate_preview(order=self._order(pack_config(1, 10000), amount_minor=10000))
        self.provider.fail = None
        GenerationJob.objects.create(  # historical moderation failure without a snapshot
            order=self.order, task_type=GenerationJob.TaskType.REVISION, status=GenerationJob.Status.FAILED,
            attempt=1, provider="fake", started_at=self.order.created_at,
            output_metadata={"failure_class": "moderation"},
        )
        QcReport.objects.create(order=self.pack9, attempt=1, status=QcReport.Status.FAILED,
                                retry_slots=[{"slot_key": "bye", "asset_id": 1}])
        QcReport.objects.create(order=self.pack9, attempt=2, status=QcReport.Status.FAILED, retry_slots=[])
        quality = PilotAnalyticsService(period_for("today")).snapshot()["quality"]
        self.assertEqual(quality["moderation_failures"], 2)
        self.assertEqual(quality["qc_retries"], 1)
        cost = gc.job_cost(GenerationJob.objects.filter(status="failed", task_type="preview").first().input_metadata)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.MODERATION_BLOCKED)

    def test_operations_block(self):
        ops = self.snapshot["operations"]
        self.assertEqual(ops["manual_minutes_total"], 25)
        self.assertEqual(ops["orders_with_manual_logs"], 2)
        self.assertEqual(ops["paid_orders_without_logs"], 2)
        self.assertEqual(ops["lead_time_orders"], 1)
        self.assertIsNotNone(ops["payment_to_delivery_hours_median"])

    def test_unit_economics_lists_every_product_from_the_db_with_its_price(self):
        rows = {row["product"]: row for row in self.snapshot["unit_economics"]}
        for code, price in ((self.single.product.code, 10000), (self.pack9.product.code, 450000),
                            (self.custom9.product.code, 720000)):
            self.assertEqual(rows[code]["price_minor"], price, code)
            self.assertEqual(rows[code]["orders"], 1)
        single = rows[self.single.product.code]
        self.assertEqual(single["revenue"], [{"currency": "RUB", "orders": 1, "amount_minor": 10000}])
        self.assertEqual(single["known_ai_cost_minor"], 2 * TARIFF)
        self.assertEqual(single["known_manual_cost_minor"], 5000)
        self.assertEqual(single["known_payment_fee_minor"], 350)
        self.assertEqual(single["known_contribution_minor"], 10000 - 1484 - 350 - 5000)
        self.assertEqual(single["orders_with_unknown"], 0)
        custom = rows[self.custom9.product.code]
        self.assertEqual(custom["known_manual_cost_minor"], 0)
        self.assertEqual(custom["unknown_counts"], {"payment_fee_unknown": 1, "manual_rate_not_configured": 1})
        xtr = rows["pack3"]
        self.assertIsNone(xtr["known_contribution_minor"])
        self.assertEqual(xtr["revenue"], [{"currency": "XTR", "orders": 1, "amount_minor": 460}])

    def test_new_product_is_not_lost(self):
        Product.objects.create(code="sticker-pack-12", name="12", config=pack_config(9, 999900))
        rows = {row["product"]: row for row in PilotAnalyticsService(period_for("today")).snapshot()["unit_economics"]}
        self.assertIn("sticker-pack-12", rows)
        self.assertEqual(rows["sticker-pack-12"]["orders"], 0)
        self.assertEqual(rows["sticker-pack-12"]["price_minor"], 999900)
        # an inactive product with orders in the period still shows up
        Product.objects.filter(pk=self.single.product.pk).update(is_active=False)
        rows = {row["product"]: row for row in PilotAnalyticsService(period_for("today")).snapshot()["unit_economics"]}
        self.assertFalse(rows[self.single.product.code]["is_active"])
        self.assertEqual(rows[self.single.product.code]["orders"], 1)

    def test_snapshot_never_calls_the_provider(self):
        calls = self.provider.calls
        PilotAnalyticsService(period_for("today")).snapshot()
        PilotAnalyticsService(period_for("today")).export_rows()
        self.client.get(reverse("admin:core_order_pilot_metrics"))
        self.assertEqual(self.provider.calls, calls)


class BudgetFlagsTests(EconomicsFixture):
    def _budget(self):
        return PilotAnalyticsService(period_for("today")).budget()

    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_DAY=100)
    def test_79_80_100_percent_flags(self):
        self._calls(78)  # + fixture preview = 79
        budget = self._budget()
        self.assertEqual(budget["today"]["utilization_percent"], 79.0)
        self.assertFalse(budget["warning"])
        self.assertFalse(budget["limit_reached"])
        self._calls(1)  # 80
        budget = self._budget()
        self.assertEqual(budget["today"]["utilization_percent"], 80.0)
        self.assertTrue(budget["today"]["warning"])
        self.assertFalse(budget["today"]["limit_reached"])
        self.assertTrue(budget["warning"])
        self._calls(20)  # 100
        budget = self._budget()
        self.assertEqual(budget["today"]["utilization_percent"], 100.0)
        self.assertTrue(budget["today"]["limit_reached"])
        self.assertTrue(budget["limit_reached"])
        response = self.client.get(reverse("admin:core_order_pilot_metrics"))
        self.assertContains(response, "лимит достигнут")

    def test_no_limit_means_no_flags(self):
        budget = self._budget()
        self.assertIsNone(budget["today"]["utilization_percent"])
        self.assertFalse(budget["warning"])
        self.assertFalse(budget["limit_reached"])


class PageAndExportTests(ThreeProductsFixture):
    def test_page_renders_blocks_in_russian_without_zeros_for_unknown(self):
        response = self.client.get(reverse("admin:core_order_pilot_metrics") + "?preset=today")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for title in ("Метрики Pilot", "Бюджет", "Заказы (события за период)", "Воронка", "Выручка", "Стоимость AI",
                      "Качество", "Операции", "Юнит-экономика по продуктам", "Экспорт CSV", "Экспорт JSON"):
            self.assertIn(title, html)
        self.assertIn("460 XTR", html)
        self.assertIn("11800,00 ₽ (3)", html)  # RUB total, apart from XTR
        self.assertIn("не вычисляется", html)  # XTR contribution
        self.assertIn("ставка не настроена: 1", html)
        self.assertIn("без цены: 1", html)
        self.assertNotIn("прибыль", html.lower())

    def test_changelist_links_to_the_page(self):
        response = self.client.get(reverse("admin:core_order_changelist"))
        self.assertContains(response, reverse("admin:core_order_pilot_metrics"))

    def test_custom_period_from_query(self):
        response = self.client.get(reverse("admin:core_order_pilot_metrics") + "?preset=custom&start=2026-09-01&end=2026-09-30")
        self.assertEqual(response.context["period"].label, "2026-09-01_2026-09-30")
        response = self.client.get(reverse("admin:core_order_pilot_metrics") + "?preset=custom&start=bad")
        self.assertEqual(response.context["period"].preset, "30d")

    def test_export_csv_columns_unknown_empty_no_pii(self):
        response = self.client.get(reverse("admin:core_order_pilot_metrics_export_csv") + "?preset=today")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"pilot-metrics_", response["Content-Disposition"])
        self.assertIn(period_for("today").label, response["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(list(rows[0].keys()), list(EXPORT_COLUMNS))
        by_id = {int(r["order_id"]): r for r in rows}
        single = by_id[self.single.pk]
        self.assertEqual(single["currency"], "RUB")
        self.assertEqual(single["revenue"], "100.00")
        self.assertEqual(single["generation_calls"], "2")
        self.assertEqual(single["preview_cost"], "7.42")
        self.assertEqual(single["full_cost"], "7.42")
        self.assertEqual(single["regeneration_cost"], "0.00")
        self.assertEqual(single["ai_total"], "14.84")
        self.assertEqual(single["manual_minutes"], "5")
        self.assertEqual(single["manual_cost"], "50.00")
        self.assertEqual(single["payment_fee"], "3.50")
        self.assertEqual(single["known_variable_cost"], "68.34")
        self.assertEqual(single["known_contribution"], "31.66")
        self.assertEqual(single["unknown_cost_components"], "")
        self.assertTrue(single["paid_at"] and single["delivered_at"] and single["created_at"])
        custom = by_id[self.custom9.pk]
        self.assertEqual(custom["manual_cost"], "")  # rate not configured → empty, not 0
        self.assertEqual(custom["payment_fee"], "")
        self.assertEqual(custom["known_contribution"], "7192.58")  # known part; gaps listed in the last column
        self.assertEqual(custom["unknown_cost_components"], "payment_fee_unknown;manual_rate_not_configured")
        xtr = by_id[self.order.pk]
        self.assertEqual(xtr["currency"], "XTR")
        self.assertEqual(xtr["revenue"], "460")
        self.assertEqual(xtr["known_contribution"], "")
        self.assertEqual(xtr["ai_total"], "")  # historical job without price
        self.assertEqual(xtr["preview_cost"], "")
        body = response.content.decode("utf-8")
        for pii in ("Анна", "+7 900", "contact", "display_name", "username", "photo"):
            self.assertNotIn(pii, body)

    def test_export_json_nulls_for_unknown(self):
        response = self.client.get(reverse("admin:core_order_pilot_metrics_export_json") + "?preset=today")
        self.assertEqual(response.status_code, 200)
        self.assertIn(".json", response["Content-Disposition"])
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["columns"], list(EXPORT_COLUMNS))
        self.assertEqual(payload["period"]["timezone"], "Europe/Moscow")
        by_id = {r["order_id"]: r for r in payload["rows"]}
        self.assertIsNone(by_id[self.custom9.pk]["manual_cost"])
        self.assertIsNone(by_id[self.custom9.pk]["payment_fee"])
        self.assertIsNone(by_id[self.order.pk]["known_contribution"])
        self.assertEqual(by_id[self.order.pk]["revenue"], 460)
        self.assertEqual(by_id[self.single.pk]["known_contribution"], "31.66")
        self.assertEqual(set(by_id[self.single.pk]), set(EXPORT_COLUMNS))


class QueryCountTests(EconomicsFixture):
    def _make(self, n):
        for i in range(n):
            order = self._order(pack_config(3, 150000), amount_minor=150000, fee_minor=5250)
            self._preview(order)
            self.production.start(order=order, max_slots=None)
            self._log_minutes(order, 5)
            OrderEvent.objects.create(order=order, event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED)

    def test_page_and_export_query_count_is_independent_of_order_count(self):
        self._make(3)
        with CaptureQueriesContext(connection) as small_page:
            self.client.get(reverse("admin:core_order_pilot_metrics"))
        with CaptureQueriesContext(connection) as small_csv:
            self.client.get(reverse("admin:core_order_pilot_metrics_export_csv"))
        with CaptureQueriesContext(connection) as small_service:
            PilotAnalyticsService(period_for("today")).snapshot()
        self._make(27)
        self.assertEqual(Order.objects.count(), 31)
        with CaptureQueriesContext(connection) as big_page:
            self.client.get(reverse("admin:core_order_pilot_metrics"))
        with CaptureQueriesContext(connection) as big_csv:
            self.client.get(reverse("admin:core_order_pilot_metrics_export_csv"))
        with CaptureQueriesContext(connection) as big_service:
            PilotAnalyticsService(period_for("today")).snapshot()
        self.assertEqual(len(big_page), len(small_page))
        self.assertEqual(len(big_csv), len(small_csv))
        self.assertEqual(len(big_service), len(small_service))
        self.assertLessEqual(len(big_service), 12)
        # measured: service 12 queries, page 14 (admin session/user), CSV 7 — for 3 and for 30 orders
