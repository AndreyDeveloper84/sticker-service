"""DRF-2111 PR-A — generation cost accounting foundation.

Every billable attempt gets an immutable price snapshot in
``GenerationJob.input_metadata["cost"]`` at creation (inside the Budget Guard
transaction, before the provider); the outcome is appended afterwards.
The owner's test list is covered one test per item.
"""

from unittest.mock import patch

import httpx
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult
from apps.core.models import GeneratedAsset, GenerationJob, Order
from apps.core.services import generation_cost as gc
from apps.core.services.budget import BudgetExceeded, BudgetService
from apps.core.services.full_production import FullProductionService
from apps.core.services.generation import GenerationError, GenerationService
from apps.core.services.pilot_metrics import PilotMetricsService
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.tests_pilot_budget import UNLIMITED, BudgetFixture
from apps.core.tests_qc import make_image

PRICED = dict(UNLIMITED, PILOT_IMAGE_CALL_COST_RUB=7.42, PILOT_IMAGE_PRICING_VERSION="pilot-tariff-1")


class CountingProvider:
    """Fake provider: counts calls, can fail with a chosen failure class."""

    name = "openai"
    model = "gpt-image-2"

    def __init__(self, *, fail=None):
        self.calls = 0
        self.fail = fail  # None | "transport" | "geo" | "moderation" | "ambiguous" | "api"

    def generate_preview(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"provider {self.fail}")
        return ImageGenerationResult(
            content=make_image(), mime_type="image/png",
            metadata={"model": self.model, "usage": {"total_tokens": 100}, "request_id": "req_success_1"},
        )

    def classify_failure(self, exc):
        return self.fail or "unknown"


def cost_of(job):
    job.refresh_from_db()
    return gc.job_cost(job.input_metadata)


class CostFixture(BudgetFixture):
    def setUp(self):
        super().setUp()
        self.provider = CountingProvider()
        self.generation = GenerationService(provider=self.provider, storage=self.storage)
        self.production = FullProductionService(provider=self.provider, storage=self.storage)

    def _paid_order(self):
        return self._make_order(self.identity.user, self.order.product, self.order.style, status=Order.Status.PAID)


@override_settings(**PRICED)
class CostSnapshotTests(CostFixture):
    def assert_priced(self, cost, *, task_type, mode=gc.MODE_INITIAL):
        self.assertEqual(cost["cost_source"], gc.CostSource.CONFIG_SNAPSHOT)
        self.assertEqual(cost["unit_price_minor"], 742)
        self.assertEqual(cost["cost_minor"], 742)
        self.assertEqual(cost["currency"], "RUB")
        self.assertEqual(cost["unit"], "call")
        self.assertEqual(cost["pricing_version"], "pilot-tariff-1")
        self.assertEqual(cost["provider"], "openai")
        self.assertEqual(cost["endpoint"], "images.edit")
        self.assertEqual(cost["model"], "gpt-image-2")
        self.assertEqual(cost["task_type"], task_type)
        self.assertEqual(cost["mode"], mode)
        self.assertTrue(cost["captured_at"])

    # 1. successful preview cost
    def test_successful_preview_cost(self):
        order = self._paid_order()
        asset = self.generation.generate_preview(order=order)
        cost = cost_of(asset.job)
        self.assert_priced(cost, task_type="preview")
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.SUCCESS)
        self.assertIs(cost["billable"], True)
        self.assertEqual(cost["request_id"], "req_success_1")
        self.assertEqual(self.provider.calls, 1)

    # 2. successful FULL cost (per slot)
    def test_successful_full_cost_per_slot(self):
        self.production.start(order=self.order, max_slots=None)
        jobs = GenerationJob.objects.filter(order=self.order, task_type=GenerationJob.TaskType.FULL).order_by("slot_key")
        self.assertEqual([job.slot_key for job in jobs], ["e0", "e1", "e2"])
        for job in jobs:
            cost = cost_of(job)
            self.assert_priced(cost, task_type="full")
            self.assertIs(cost["billable"], True)
            self.assertIsNone(cost["qc_attempt"])
        total = BudgetService().order_costs(self.order)["cost"]
        self.assertEqual(total["known_cost_minor"], 3 * 742)
        self.assertEqual(total["known_count"], 3)

    # 3. revision cost
    def test_revision_cost(self):
        from apps.core.models import Revision

        order = self._make_order(self.identity.user, self.order.product, self.order.style,
                                 status=Order.Status.REVISION_REQUESTED)
        preview = GeneratedAsset.objects.get(order=order, kind=GeneratedAsset.Kind.PREVIEW)
        Revision.objects.create(order=order, source_preview=preview, category=Revision.Category.FACE)

        revised = self.generation.generate_revision(order=order)

        cost = cost_of(revised.job)
        self.assert_priced(cost, task_type="revision")
        self.assertIs(cost["billable"], True)

    # 4. regeneration cost (mode=regenerate, QC attempt recorded)
    def test_regeneration_cost_carries_mode_and_qc_attempt(self):
        self.production.start(order=self.order, max_slots=None)
        qc = QcService(storage=self.storage)
        report = qc.start_qc(order=self.order)
        checklist = {criterion: True for criterion in HUMAN_CRITERIA}
        checklist[next(iter(HUMAN_CRITERIA))] = False
        qc.finalize_report(report=report, checklist=checklist)
        report.refresh_from_db()
        qc.request_retry(report=report, slot_keys=["e1"])

        self.order.refresh_from_db()
        # only the slot QC sent back carries the QC attempt marker
        self.assertEqual(FullProductionService._qc_retry_attempt(self.order, "e1"), report.attempt)
        self.assertIsNone(FullProductionService._qc_retry_attempt(self.order, "e0"))

        self.production.regenerate_slots(order=self.order, slot_keys=["e1"])

        job = GenerationJob.objects.filter(order=self.order, slot_key="e1").order_by("-attempt").first()
        cost = cost_of(job)
        self.assert_priced(cost, task_type="full", mode=gc.MODE_REGENERATE)
        self.assertEqual(cost["qc_attempt"], report.attempt)
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)

    def test_retry_failed_and_force_retry_modes(self):
        self.provider.fail = "api"
        self.production.start(order=self.order)  # e0 fails with api_error
        self.provider.fail = None
        self.production.retry_failed(order=self.order)
        retried = GenerationJob.objects.filter(order=self.order, slot_key="e0").order_by("-attempt").first()
        self.assertEqual(cost_of(retried)["mode"], gc.MODE_RETRY_FAILED)

        self.provider.fail = "ambiguous"
        self.production.start(order=self.order)  # e1 → ambiguous → blocked
        self.provider.fail = None
        self.production.force_retry_slot(order=self.order, slot_key="e1")
        forced = GenerationJob.objects.filter(order=self.order, slot_key="e1").order_by("-attempt").first()
        self.assertEqual(cost_of(forced)["mode"], gc.MODE_FORCE_RETRY)

    # 5. price snapshot survives a config change
    def test_price_snapshot_survives_config_change(self):
        """ANSWER TO «может ли изменение тарифа завтра изменить стоимость
        старого job?» — NO: the snapshot is immutable and no reader re-prices."""
        order = self._paid_order()
        asset = self.generation.generate_preview(order=order)
        before = cost_of(asset.job)
        before_costs = BudgetService().order_costs(order)["cost"]
        self.assertEqual(before["cost_minor"], 742)
        self.assertEqual(before_costs["known_cost_minor"], 742)

        with override_settings(PILOT_IMAGE_CALL_COST_RUB=99.0, PILOT_IMAGE_PRICING_VERSION="pilot-tariff-2"):
            after = cost_of(asset.job)
            self.assertEqual(after, before)
            self.assertEqual(BudgetService().order_costs(order)["cost"]["known_cost_minor"], 742)
            self.assertEqual(BudgetService().summary()["today"]["cost"]["known_cost_minor"], 742)
            self.assertEqual(PilotMetricsService().snapshot()["generation_cost"]["known_cost_minor"], 742)
            # a NEW job under the new tariff is priced with the new tariff
            fresh = self.generation.generate_preview(order=self._paid_order())
            self.assertEqual(cost_of(fresh.job)["cost_minor"], 9900)
            self.assertEqual(cost_of(fresh.job)["pricing_version"], "pilot-tariff-2")
        # even after the tariff is removed entirely, the old job keeps its price
        with override_settings(PILOT_IMAGE_CALL_COST_RUB=None):
            self.assertEqual(cost_of(asset.job)["cost_minor"], 742)

    def test_outcome_update_cannot_touch_price_fields(self):
        snapshot = gc.cost_snapshot(provider=self.provider, task_type="preview")
        tampered = gc.with_outcome({**snapshot, "cost_minor": 1}, outcome=gc.BillingOutcome.SUCCESS, billable=True)
        # with_outcome copies price fields verbatim from its input and only sets outcome keys
        self.assertEqual(tampered["cost_minor"], 1)
        self.assertEqual({k for k in tampered if k not in snapshot}, {"outcome_at"})
        for key in gc.PRICE_FIELDS:
            if key in snapshot:  # "pricing" exists on ESTIMATED snapshots only
                self.assertEqual(tampered[key], {**snapshot, "cost_minor": 1}[key], key)

    # 6. unknown price → UNKNOWN (not 0, no default)
    @override_settings(PILOT_IMAGE_CALL_COST_RUB=None, PILOT_IMAGE_PRICING_VERSION=None)
    def test_unknown_price_is_unknown_not_zero(self):
        with patch.dict("os.environ", {"PILOT_IMAGE_CALL_COST_RUB": ""}):
            order = self._paid_order()
            asset = self.generation.generate_preview(order=order)
        cost = cost_of(asset.job)
        self.assertEqual(cost["cost_source"], gc.CostSource.UNKNOWN)
        self.assertIsNone(cost["cost_minor"])
        self.assertIsNone(cost["unit_price_minor"])
        self.assertIsNone(cost["pricing_version"])
        self.assertIs(cost["billable"], True)  # the outcome is known, the price is not
        summary = BudgetService().order_costs(order)["cost"]
        self.assertEqual(summary["known_cost_minor"], 0)
        self.assertEqual(summary["known_count"], 0)
        self.assertEqual(summary["unknown_price_count"], 2)  # this job + fixture's historical preview
        self.assertEqual(summary["possibly_billable_count"], 1)  # only the historical one

    def test_default_pricing_version_is_price_at_date(self):
        with override_settings(PILOT_IMAGE_PRICING_VERSION=None), patch.dict("os.environ", {"PILOT_IMAGE_PRICING_VERSION": ""}):
            snapshot = gc.cost_snapshot(provider=self.provider, task_type="preview")
        self.assertEqual(snapshot["pricing_version"], f"7.42@{timezone.localdate().isoformat()}")

    # 7. failed before provider → billable=false
    def test_failed_before_provider_is_not_billable(self):
        for failure_class in ("transport", "geo"):
            self.provider.fail = failure_class
            order = self._paid_order()
            with self.assertRaises(GenerationError):
                self.generation.generate_preview(order=order)
            job = GenerationJob.objects.get(order=order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.FAILED)
            cost = cost_of(job)
            self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.BEFORE_PROVIDER, failure_class)
            self.assertIs(cost["billable"], False)
            self.assertEqual(cost["cost_minor"], 742, "price snapshot kept; effective cost 0 via billable=false")
            summary = BudgetService().order_costs(order)["cost"]
            self.assertEqual(summary["not_billable_count"], 1)
            self.assertEqual(summary["known_cost_minor"], 0)

    # 8. moderation_blocked → billable=null + stage
    def test_moderation_blocked_is_unknown_billing_with_stage(self):
        self.provider.fail = "moderation"
        order = self._paid_order()
        with patch("apps.core.services.generation.describe_provider_failure",
                   return_value={"failure_class": "moderation", "moderation_stage": "output",
                                 "moderation_categories": ["sexual"], "request_id": "req_mod_1"}):
            with self.assertRaises(GenerationError):
                self.generation.generate_preview(order=order)
        job = GenerationJob.objects.get(order=order, status=GenerationJob.Status.FAILED)
        cost = cost_of(job)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.MODERATION_BLOCKED)
        self.assertIsNone(cost["billable"])
        self.assertEqual(cost["moderation_stage"], "output")
        self.assertEqual(cost["request_id"], "req_mod_1")
        # + the fixture's historical preview job (no snapshot) → 2 possibly billable
        self.assertEqual(BudgetService().order_costs(order)["cost"]["possibly_billable_count"], 2)

    # 9. ambiguous → billable=null
    def test_ambiguous_failure_is_unknown_billing(self):
        self.provider.fail = "ambiguous"
        self.production.start(order=self.order)
        job = GenerationJob.objects.get(order=self.order, task_type=GenerationJob.TaskType.FULL)
        cost = cost_of(job)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.TIMEOUT_AMBIGUOUS)
        self.assertIsNone(cost["billable"])

    def test_stale_running_job_is_unknown_billing(self):
        order = self._paid_order()
        stale = GenerationJob.objects.create(
            order=order, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.RUNNING,
            attempt=2, provider="openai", started_at=timezone.now() - timezone.timedelta(hours=3),
            input_metadata={gc.COST_KEY: gc.cost_snapshot(provider=self.provider, task_type="preview")},
        )
        self.generation.generate_preview(order=order)  # the RUNNING guard fails the stale job closed
        cost = cost_of(stale)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.TIMEOUT_AMBIGUOUS)
        self.assertIsNone(cost["billable"])

    # 10. api_error → billable=null
    def test_api_error_is_unknown_billing(self):
        self.provider.fail = "api"
        order = self._paid_order()
        with self.assertRaises(GenerationError):
            self.generation.generate_preview(order=order)
        job = GenerationJob.objects.get(order=order, status=GenerationJob.Status.FAILED)
        cost = cost_of(job)
        self.assertEqual(cost["billing_outcome"], gc.BillingOutcome.API_ERROR)
        self.assertIsNone(cost["billable"])

    # 11. historical job without snapshot → UNKNOWN, not 0 (no backfill)
    def test_historical_job_without_snapshot_is_unknown(self):
        self._calls(2)  # fixture jobs: no cost key at all
        for job in GenerationJob.objects.filter(order=self.order):
            self.assertIsNone(gc.job_cost(job.input_metadata))
        summary = BudgetService().order_costs(self.order)["cost"]
        self.assertEqual(summary["jobs"], 3)
        self.assertEqual(summary["known_cost_minor"], 0)
        self.assertEqual(summary["unknown_price_count"], 3)
        self.assertEqual(summary["possibly_billable_count"], 3)
        # completing a historical job later does not invent a price either
        job = GenerationJob.objects.filter(order=self.order).first()
        gc.apply_success(job, {"request_id": "x"})
        self.assertNotIn(gc.COST_KEY, job.input_metadata)

    # 12. analytics / console never invoke the provider
    def test_analytics_and_console_never_call_the_provider(self):
        self.production.start(order=self.order, max_slots=None)
        calls_before = self.provider.calls
        BudgetService().order_costs(self.order)
        BudgetService().summary()
        PilotMetricsService().snapshot()
        with patch.object(type(self.production), "start") as start_spy:
            self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
            self.client.get(reverse("admin:core_order_changelist"))
            start_spy.assert_not_called()
        self.assertEqual(self.provider.calls, calls_before)

    # 13. Budget Guard refusal → no job, no snapshot, no provider call
    @override_settings(PILOT_MAX_IMAGE_CALLS_PER_ORDER=1)
    def test_budget_guard_refusal_leaves_no_snapshot(self):
        order = self._paid_order()  # fixture preview = 1 call → limit reached
        with self.assertRaises(BudgetExceeded):
            self.generation.generate_preview(order=order)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(GenerationJob.objects.filter(order=order).count(), 1)
        self.assertEqual(BudgetService().order_costs(order)["cost"]["jobs"], 1)

    # 14. mixed set aggregation
    def test_mixed_set_aggregation(self):
        self.production.start(order=self.order)  # e0 success, priced
        self.provider.fail = "transport"
        self.production.start(order=self.order)  # e1 before_provider
        self.provider.fail = "api"
        self.production.retry_failed(order=self.order)  # e1 retry → api_error (unknown billing)
        self.provider.fail = None
        self._calls(1, task=GenerationJob.TaskType.REVISION)  # historical, no snapshot

        summary = BudgetService().order_costs(self.order)["cost"]

        self.assertEqual(summary["jobs"], 5)  # fixture preview + 4 above
        self.assertEqual(summary["known_count"], 1)
        self.assertEqual(summary["known_cost_minor"], 742)
        self.assertEqual(summary["not_billable_count"], 1)
        self.assertEqual(summary["possibly_billable_count"], 3)  # api_error + 2 historical
        self.assertEqual(summary["unknown_price_count"], 2)  # the 2 historical jobs
        self.assertEqual(gc.format_known_cost(summary), "7,42 ₽ (известно по 1 вызовам)")

    def test_console_shows_known_cost_and_possibly_billable(self):
        self.production.start(order=self.order)
        self.provider.fail = "api"
        self.production.start(order=self.order)
        response = self.client.get(reverse("admin:core_order_change", args=[self.order.pk]))
        # the cost lives in «Экономика заказа» only (one block, owner wording 2026-09-20)
        self.assertContains(response, "AI всего: известно: 7,42 ₽ + 2 вызовов с неизвестной стоимостью")  # api_error + historical
        self.assertNotContains(response, "стоимость: 7,42")
        self.assertContains(response, "возможно платных: 2")  # api_error + fixture preview (historical)
        response = self.client.get(reverse("admin:core_order_changelist"))
        self.assertContains(response, "стоимость 7,42 ₽ (известно по 1 вызовам)")

    def test_snapshot_lives_in_budget_guard_transaction_before_provider(self):
        """The snapshot exists on the RUNNING job before the provider answers."""
        seen = {}

        class PeekProvider(CountingProvider):
            def generate_preview(self_, request):
                job = GenerationJob.objects.get(status=GenerationJob.Status.RUNNING)
                seen["cost"] = gc.job_cost(job.input_metadata)
                return super().generate_preview(request)

        order = self._paid_order()
        GenerationService(provider=PeekProvider(), storage=self.storage).generate_preview(order=order)
        self.assertEqual(seen["cost"]["billing_outcome"], gc.BillingOutcome.PENDING)
        self.assertIsNone(seen["cost"]["billable"])
        self.assertEqual(seen["cost"]["cost_minor"], 742)


class OpenAIRequestIdTests(CostFixture):
    def test_openai_provider_records_request_id_on_success(self):
        from types import SimpleNamespace

        from apps.core.image_providers import OpenAIImageProvider

        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json="aGVsbG8=")], usage=None, _request_id="req_abc",
        )
        client = SimpleNamespace(images=SimpleNamespace(edit=lambda **kwargs: response))
        provider = OpenAIImageProvider(client=client, proxy_pool=None)
        result = provider._generate(client, [], "prompt")
        self.assertEqual(result.metadata["request_id"], "req_abc")
