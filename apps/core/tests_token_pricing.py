"""ESTIMATED generation cost from token usage (owner GO 2026-09-20).

Rates and formula: docs/PRICING-gpt-image-2-token-rates-2026-09-20.md.
The provider keeps ``usage.input_tokens_details``; the snapshot taken at
attempt creation carries the rates and the fx (immutable); after the answer
``usd = text×r_t + image×r_i + out×r_o`` (per 1M) and ``rub = usd × fx``
(only with an fx in the snapshot); no split → UNKNOWN, never an upper bound.

Synthetic example checked by hand (report): text 220 / image 3 050 /
output 1 929 at 5 / 8 / 30 and fx 90,00 →
usd = 0.001100 + 0.024400 + 0.057870 = 0.083370 → rub = 7,5033 → 7,50 ₽.
"""

import csv
import io
import re
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from apps.core.image_providers import ImageGenerationResult, _usage_metadata
from apps.core.models import GenerationJob, Order
from apps.core.services import generation_cost as gc
from apps.core.services.budget import BudgetService
from apps.core.services.order_economics import OrderEconomics
from apps.core.services.pilot_analytics import EXPORT_COLUMNS, PilotAnalyticsService, period_for
from apps.core.tests_generation_cost import CostFixture, cost_of
from apps.core.tests_order_economics import EconomicsFixture, fee_metadata, pack_config
from apps.core.tests_pilot_budget import UNLIMITED
from apps.core.tests_qc import make_image

RATES = dict(
    PILOT_TOKEN_PRICING_MODEL="gpt-image-2", PILOT_TOKEN_PRICING_VERSION="openai-pricing@2026-09-20",
    PILOT_TEXT_INPUT_USD_PER_1M=5.0, PILOT_IMAGE_INPUT_USD_PER_1M=8.0, PILOT_IMAGE_OUTPUT_USD_PER_1M=30.0,
)
FX = dict(PILOT_FX_USD_RUB=90.0, PILOT_FX_DATE="2026-09-20", PILOT_FX_SOURCE="cbr.ru")
TOKEN_PRICED = dict(UNLIMITED, PILOT_IMAGE_CALL_COST_RUB=None, PILOT_IMAGE_PRICING_VERSION=None, **RATES, **FX)
USAGE = {"input_tokens": 3270, "output_tokens": 1929, "total_tokens": 5199,
         "input_tokens_details": {"text_tokens": 220, "image_tokens": 3050}}
USD = 0.083370
RUB_MINOR = 750


class TokenProvider:
    """Fake provider reporting the real usage shape (with the input split)."""

    name = "openai"
    model = "gpt-image-2"

    def __init__(self, usage=None):
        self.calls = 0
        self.usage = USAGE if usage is None else usage
        self.fail = None

    def generate_preview(self, request):
        self.calls += 1
        metadata = {"model": self.model, "request_id": f"req_{self.calls}"}
        if self.usage is not None:
            metadata["usage"] = dict(self.usage)
        return ImageGenerationResult(content=make_image(), mime_type="image/png", metadata=metadata)

    def classify_failure(self, exc):
        return "unknown"


# ------------------------------------------------------------ pure functions


class UsageDetailsTests(SimpleTestCase):
    def test_provider_keeps_the_input_split_from_sdk_object_and_dict(self):
        sdk = SimpleNamespace(input_tokens=3270, output_tokens=1929, total_tokens=5199,
                              input_tokens_details=SimpleNamespace(text_tokens=220, image_tokens=3050))
        self.assertEqual(_usage_metadata(sdk), USAGE)
        self.assertEqual(_usage_metadata(USAGE), USAGE)

    def test_missing_split_is_not_invented(self):
        self.assertEqual(_usage_metadata({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
                         {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        self.assertEqual(_usage_metadata({"input_tokens": 10, "input_tokens_details": {"text_tokens": "x"}}),
                         {"input_tokens": 10})
        self.assertIsNone(gc.usage_tokens({"input_tokens": 10, "output_tokens": 5}))

    def test_formula_on_the_synthetic_example(self):
        pricing = {"text_input_rate_usd_per_1m": 5.0, "image_input_rate_usd_per_1m": 8.0,
                   "image_output_rate_usd_per_1m": 30.0, "fx_usd_rub": 90.0}
        tokens = gc.usage_tokens(USAGE)
        self.assertEqual(tokens, {"text_input": 220, "image_input": 3050, "output": 1929})
        self.assertEqual(gc.estimate_usd(tokens, pricing), USD)  # 0.0011 + 0.0244 + 0.05787
        self.assertEqual(gc.estimate_rub_minor(USD, pricing), RUB_MINOR)  # 7.5033 → 7,50 ₽
        self.assertIsNone(gc.estimate_rub_minor(USD, {**pricing, "fx_usd_rub": None}))
        # half-up rounding of the kopecks, 6-decimal USD
        self.assertEqual(gc.estimate_rub_minor(0.005, {"fx_usd_rub": 1.0}), 1)  # 0.5 kopecks → 1
        self.assertEqual(gc.estimate_usd({"text_input": 1, "image_input": 1, "output": 1}, pricing), 0.000043)

    def test_token_pricing_config_fail_closed(self):
        with override_settings(**RATES, **FX):
            pricing = gc.token_pricing()
            self.assertEqual(pricing["model"], "gpt-image-2")
            self.assertEqual(pricing["pricing_version"], "openai-pricing@2026-09-20")
            self.assertEqual(pricing["text_input_rate_usd_per_1m"], 5.0)
            self.assertEqual(pricing["fx_usd_rub"], 90.0)
            self.assertEqual(pricing["fx_source"], "cbr.ru")
        with override_settings(**{**RATES, "PILOT_IMAGE_OUTPUT_USD_PER_1M": "thirty"}):
            self.assertIsNone(gc.token_pricing())  # an invalid rate → no token pricing at all
        with override_settings(**{**RATES, "PILOT_IMAGE_OUTPUT_USD_PER_1M": None}), \
                patch.dict("os.environ", {"PILOT_IMAGE_OUTPUT_USD_PER_1M": ""}):
            self.assertIsNone(gc.token_pricing())  # a missing rate → none
        with override_settings(**{**RATES, "PILOT_FX_USD_RUB": "ninety"}):
            pricing = gc.token_pricing()
            self.assertIsNotNone(pricing)
            self.assertIsNone(pricing["fx_usd_rub"])  # an invalid fx → USD only
        with override_settings(PILOT_TEXT_INPUT_USD_PER_1M=None, PILOT_IMAGE_INPUT_USD_PER_1M=None,
                               PILOT_IMAGE_OUTPUT_USD_PER_1M=None), \
                patch.dict("os.environ", {"PILOT_TEXT_INPUT_USD_PER_1M": "", "PILOT_IMAGE_INPUT_USD_PER_1M": "",
                                          "PILOT_IMAGE_OUTPUT_USD_PER_1M": ""}):
            self.assertIsNone(gc.token_pricing())


# ------------------------------------------------------------ snapshot + services


@override_settings(**TOKEN_PRICED)
class EstimatedSnapshotTests(CostFixture):
    def setUp(self):
        super().setUp()
        self.provider = TokenProvider()
        from apps.core.services.generation import GenerationService
        self.generation = GenerationService(provider=self.provider, storage=self.storage)

    def test_snapshot_carries_rates_and_fx_and_the_estimate_is_computed_after_the_answer(self):
        snapshot = gc.cost_snapshot(provider=self.provider, task_type="preview")
        self.assertEqual(snapshot["cost_source"], gc.CostSource.ESTIMATED)
        self.assertEqual(snapshot["unit"], "token")
        self.assertIsNone(snapshot["cost_minor"])
        self.assertEqual(snapshot["pricing_version"], "openai-pricing@2026-09-20")
        pricing = snapshot["pricing"]
        self.assertEqual(
            {k: pricing[k] for k in ("model", "pricing_version", "text_input_rate_usd_per_1m",
                                     "image_input_rate_usd_per_1m", "image_output_rate_usd_per_1m",
                                     "fx_usd_rub", "fx_date", "fx_source")},
            {"model": "gpt-image-2", "pricing_version": "openai-pricing@2026-09-20",
             "text_input_rate_usd_per_1m": 5.0, "image_input_rate_usd_per_1m": 8.0,
             "image_output_rate_usd_per_1m": 30.0, "fx_usd_rub": 90.0, "fx_date": "2026-09-20", "fx_source": "cbr.ru"},
        )
        self.assertTrue(pricing["captured_at"])

        asset = self.generation.generate_preview(order=self._paid_order())
        cost = cost_of(asset.job)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.SUCCESS)
        self.assertIs(cost["billable"], True)
        self.assertEqual(cost["tokens"], {"text_input": 220, "image_input": 3050, "output": 1929})
        self.assertEqual(cost["usd_estimate"], USD)
        self.assertEqual(cost["rub_estimate_minor"], RUB_MINOR)
        self.assertEqual(cost["cost_minor"], RUB_MINOR)
        self.assertIs(cost["usage_split_unknown"], False)
        self.assertTrue(gc.is_known(cost))
        self.assertEqual(BudgetService().order_costs(asset.job.order)["cost"]["known_cost_minor"], RUB_MINOR)

    def test_per_call_tariff_is_ignored_when_token_pricing_is_configured(self):
        with override_settings(PILOT_IMAGE_CALL_COST_RUB=7.42), self.assertLogs("apps.core.services.generation_cost", "WARNING") as logs:
            snapshot = gc.cost_snapshot(provider=self.provider, task_type="preview")
        self.assertEqual(snapshot["cost_source"], gc.CostSource.ESTIMATED)
        self.assertIsNone(snapshot["unit_price_minor"])
        self.assertTrue(any("PILOT_IMAGE_CALL_COST_RUB is ignored" in line for line in logs.output))

    def test_rates_and_fx_change_tomorrow_never_reprice_an_old_job(self):
        asset = self.generation.generate_preview(order=self._paid_order())
        before = cost_of(asset.job)
        self.assertEqual(before["cost_minor"], RUB_MINOR)
        with override_settings(PILOT_IMAGE_OUTPUT_USD_PER_1M=40.0, PILOT_FX_USD_RUB=100.0, PILOT_FX_DATE="2026-09-21",
                               PILOT_TOKEN_PRICING_VERSION="openai-pricing@2026-09-21"):
            self.assertEqual(cost_of(asset.job), before)
            self.assertEqual(BudgetService().order_costs(asset.job.order)["cost"]["known_cost_minor"], RUB_MINOR)
            fresh = self.generation.generate_preview(order=self._paid_order())
            new = cost_of(fresh.job)
            self.assertEqual(new["pricing"]["fx_usd_rub"], 100.0)
            self.assertEqual(new["pricing"]["pricing_version"], "openai-pricing@2026-09-21")
            # 220×5 + 3050×8 + 1929×40 = 0.0011 + 0.0244 + 0.07716 = 0.10266 $ × 100 ₽ = 10,27 ₽
            self.assertEqual(new["usd_estimate"], 0.10266)
            self.assertEqual(new["cost_minor"], 1027)
        with override_settings(PILOT_TEXT_INPUT_USD_PER_1M=None), patch.dict("os.environ", {"PILOT_TEXT_INPUT_USD_PER_1M": ""}):
            self.assertEqual(cost_of(asset.job)["cost_minor"], RUB_MINOR)  # tariff removed: the old job keeps its estimate

    def test_without_fx_the_usd_stays_and_rub_is_unknown(self):
        with override_settings(PILOT_FX_USD_RUB=None), patch.dict("os.environ", {"PILOT_FX_USD_RUB": ""}):
            asset = self.generation.generate_preview(order=self._paid_order())
        cost = cost_of(asset.job)
        self.assertEqual(cost["cost_source"], gc.CostSource.ESTIMATED)
        self.assertIsNone(cost["pricing"]["fx_usd_rub"])
        self.assertEqual(cost["usd_estimate"], USD)
        self.assertIsNone(cost["rub_estimate_minor"])
        self.assertIsNone(cost["cost_minor"])
        self.assertFalse(gc.is_known(cost))
        summary = BudgetService().order_costs(asset.job.order)["cost"]
        self.assertEqual(summary["known_count"], 0)
        self.assertEqual(summary["unknown_price_count"], 2)  # this estimate without RUB + the fixture's historical preview
        self.assertEqual(summary["usd_estimate_total"], USD)
        self.assertEqual(summary["usd_known_count"], 1)
        self.assertEqual(gc.format_known_cost(summary), "неизвестна")

    def test_without_the_input_split_the_cost_is_unknown_not_an_upper_bound(self):
        self.provider.usage = {"input_tokens": 3270, "output_tokens": 1929, "total_tokens": 5199}
        asset = self.generation.generate_preview(order=self._paid_order())
        cost = cost_of(asset.job)
        self.assertEqual(cost["cost_source"], gc.CostSource.ESTIMATED)
        self.assertIs(cost["usage_split_unknown"], True)
        self.assertIsNone(cost["usd_estimate"])
        self.assertIsNone(cost["cost_minor"])
        self.assertFalse(gc.is_known(cost))
        summary = BudgetService().order_costs(asset.job.order)["cost"]
        self.assertEqual(summary["unknown_price_count"], 2)  # this one + the fixture's historical preview
        self.assertEqual(summary["usd_known_count"], 0)
        self.assertEqual(gc.format_known_cost(summary), "неизвестна")

    def test_no_token_pricing_keeps_the_old_behaviour(self):
        with override_settings(PILOT_TEXT_INPUT_USD_PER_1M=None, PILOT_IMAGE_INPUT_USD_PER_1M=None,
                               PILOT_IMAGE_OUTPUT_USD_PER_1M=None, PILOT_IMAGE_CALL_COST_RUB=7.42), \
                patch.dict("os.environ", {"PILOT_TEXT_INPUT_USD_PER_1M": "", "PILOT_IMAGE_INPUT_USD_PER_1M": "",
                                          "PILOT_IMAGE_OUTPUT_USD_PER_1M": ""}):
            per_call = gc.cost_snapshot(provider=self.provider, task_type="preview")
            self.assertEqual(per_call["cost_source"], gc.CostSource.CONFIG_SNAPSHOT)
            self.assertNotIn("pricing", per_call)
            with override_settings(PILOT_IMAGE_CALL_COST_RUB=None):
                unknown = gc.cost_snapshot(provider=self.provider, task_type="preview")
                self.assertEqual(unknown["cost_source"], gc.CostSource.UNKNOWN)

    def test_aggregate_of_a_mixed_set(self):
        estimated = {"cost": {**gc.cost_snapshot(provider=self.provider, task_type="preview"), "billable": True,
                              "billing_outcome": "success"}}
        estimated["cost"].update(gc.estimate_from_usage(estimated["cost"], USAGE))
        no_split = {"cost": {**gc.cost_snapshot(provider=self.provider, task_type="preview"), "billable": True,
                             "billing_outcome": "success"}}
        no_split["cost"].update(gc.estimate_from_usage(no_split["cost"], {"output_tokens": 1}))
        with override_settings(PILOT_TEXT_INPUT_USD_PER_1M=None, PILOT_IMAGE_INPUT_USD_PER_1M=None,
                               PILOT_IMAGE_OUTPUT_USD_PER_1M=None, PILOT_IMAGE_CALL_COST_RUB=7.42), \
                patch.dict("os.environ", {"PILOT_TEXT_INPUT_USD_PER_1M": "", "PILOT_IMAGE_INPUT_USD_PER_1M": "",
                                          "PILOT_IMAGE_OUTPUT_USD_PER_1M": ""}):
            config = {"cost": {**gc.cost_snapshot(provider=self.provider, task_type="preview"), "billable": True}}
        rejected = {"cost": {**gc.cost_snapshot(provider=self.provider, task_type="preview"), "billable": False}}
        summary = gc.aggregate([estimated, no_split, config, rejected, None])
        self.assertEqual(summary["jobs"], 5)
        self.assertEqual(summary["known_count"], 2)
        self.assertEqual(summary["estimated_count"], 1)
        self.assertEqual(summary["known_estimated_minor"], RUB_MINOR)
        self.assertEqual(summary["known_config_minor"], 742)
        self.assertEqual(summary["known_cost_minor"], RUB_MINOR + 742)
        self.assertEqual(summary["usd_estimate_total"], USD)
        self.assertEqual(summary["usd_known_count"], 1)
        self.assertEqual(summary["tokens"], {"text_input": 220, "image_input": 3050, "output": 1929})
        self.assertEqual(summary["not_billable_count"], 1)
        self.assertEqual(summary["unknown_price_count"], 2)  # no split + historical
        self.assertEqual(gc.format_known_cost(summary), "≈ 14,92 ₽ (известно по 2 вызовам)")
        self.assertEqual(gc.format_ai_total(summary), "известно: ≈ 14,92 ₽ + 2 вызовов с неизвестной стоимостью")
        self.assertEqual(gc.format_ai_total({**summary, "unknown_price_count": 0, "jobs": 2}), "≈ 14,92 ₽")
        self.assertEqual(gc.format_known_cost({**summary, "estimated_count": 0, "jobs": 2}), "14,92 ₽")


# ------------------------------------------------------------ console / export / analytics


@override_settings(**TOKEN_PRICED)
class EstimatedConsoleTests(EconomicsFixture):
    def setUp(self):
        super().setUp()
        self.provider = TokenProvider()
        from apps.core.services.full_production import FullProductionService
        from apps.core.services.generation import GenerationService
        self.generation = GenerationService(provider=self.provider, storage=self.storage)
        self.production = FullProductionService(provider=self.provider, storage=self.storage)

    def test_order_card_shows_estimates_with_tokens_caption_and_never_a_zero(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        eco = OrderEconomics.compute(order)
        self.assertEqual(eco["ai"]["total"]["known_cost_minor"], 2 * RUB_MINOR)
        self.assertEqual(eco["ai"]["total"]["estimated_count"], 2)
        self.assertEqual(eco["ai"]["total"]["usd_estimate_total"], round(2 * USD, 6))
        self.assertEqual(eco["known_variable_cost_minor"], 2 * RUB_MINOR + 350)
        self.assertEqual(eco["known_contribution_minor"], 10000 - 2 * RUB_MINOR - 350)
        self.assertEqual(eco["unknown_components"], [])

        block = self._block(self._card(order))
        self.assertIn("превью: 1 вызов(ов), tokens in 3270 / out 1929, ≈ 7,50 ₽", block)
        self.assertIn("производство: 1 вызов(ов), tokens in 3270 / out 1929, ≈ 7,50 ₽", block)
        self.assertIn("Слоты: «Hello» #1 — ≈ 7,50 ₽", block)
        self.assertIn("AI всего: ≈ 15,00 ₽", block)
        self.assertIn("Оценка по фактическому usage (gpt-image-2, тариф openai-pricing@2026-09-20, "
                      "курс 90,00 ₽/$ от 2026-09-20, cbr.ru)", block)
        self.assertIn("Известные переменные расходы: 18,50 ₽", block)
        self.assertIn("Известный contribution: 81,50 ₽", block)
        # «Расходы» no longer repeats the cost: one block, per the owner
        html = self._card(order).content.decode()
        start = html.index("вызовов: 2 (превью 1")
        self.assertNotIn("стоимость:", html[start: html.index("</div>", start)])

    def test_partially_unknown_order_names_the_unknown_calls(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self._preview(order)  # estimated
        self.provider.usage = {"input_tokens": 3270, "output_tokens": 1929}  # no split → UNKNOWN
        self.production.start(order=order, max_slots=None)
        block = self._block(self._card(order))
        self.assertIn("AI всего: известно: ≈ 7,50 ₽ + 1 вызовов с неизвестной стоимостью", block)
        self.assertIn("Слоты: «Hello» #1 — неизвестна", block)
        self.assertIn("+ не учтено: AI-вызовов без цены: 1", block)
        self.assertIsNone(re.search(r"(?<![\d,])0(,00)? ₽", block), block)

    def test_export_and_analytics_carry_the_estimates(self):
        order = self._order(pack_config(1, 10000), amount_minor=10000, fee_minor=350)
        self._preview(order)
        self.production.start(order=order, max_slots=None)
        response = self.client.get(reverse("admin:core_order_pilot_metrics_export_csv") + "?preset=today")
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(list(rows[0].keys()), list(EXPORT_COLUMNS))
        self.assertEqual(EXPORT_COLUMNS[-1], "ai_usd_estimate")
        row = {int(r["order_id"]): r for r in rows}[order.pk]
        self.assertEqual(row["preview_cost"], "7.50")
        self.assertEqual(row["full_cost"], "7.50")
        self.assertEqual(row["ai_total"], "15.00")
        self.assertEqual(row["ai_usd_estimate"], "0.166740")
        self.assertEqual(row["known_variable_cost"], "18.50")
        snapshot = PilotAnalyticsService(period_for("today")).snapshot()
        self.assertEqual(snapshot["ai_cost"]["total"]["known_estimated_minor"], 2 * RUB_MINOR)
        self.assertEqual(snapshot["ai_cost"]["total"]["usd_estimate_total"], round(2 * USD, 6))
        self.assertEqual(snapshot["ai_cost"]["stages"]["preview"]["estimated_count"], 1)
        page = self.client.get(reverse("admin:core_order_pilot_metrics") + "?preset=today")
        # the fixture's own order adds a historical preview (no snapshot) to the period
        self.assertTrue(dict(page.context["ai_rows"])["Всего"].startswith("≈ 15,00 ₽ (известно по 2 вызовам)"))
        self.assertIn("≈ 7,50 ₽", dict(page.context["ai_rows"])["По стадиям"])
