"""DRF-2055: pilot metrics contract.

Event emission (status transitions, customer approval, manual work), provider
usage capture, the snapshot service and the management command.
"""

import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core import tests_full_generation as _full_generation
from apps.core.image_providers import ImageGenerationResult, OpenAIImageProvider
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    ManualWorkLog,
    Order,
    OrderEvent,
    Product,
    Revision,
    Style,
    User,
)
from apps.core.services.order_state import OrderStateService
from apps.core.services.payment import PaymentService
from apps.core.services.pilot_metrics import PilotMetricsService
from apps.core.services.preview_feedback import PreviewFeedbackService


class MetricsFixtureMixin:
    def setUp(self):
        self.product = Product.objects.create(code="sticker-pack-9", name="Pack", config={"price_minor": 50000, "price_stars": 460})
        self.style = Style.objects.create(code="comic", name="Comic")
        self._identity_seq = 0

    def identity(self, channel=ChannelIdentity.Channel.TELEGRAM):
        self._identity_seq += 1
        return ChannelIdentity.objects.create(
            user=User.objects.create(), channel=channel, external_user_id=f"u-{self._identity_seq}"
        )

    def order(self, status=Order.Status.DRAFT, identity=None):
        identity = identity or self.identity()
        return Order.objects.create(
            user=identity.user, channel_identity=identity, product=self.product, style=self.style, status=status
        )

    def walk(self, order, *statuses):
        for status in statuses:
            if status == Order.Status.PAID:
                payment = PaymentService.create_pending(order=order, provider="telegram_stars", amount_minor=460, currency="XTR")
                PaymentService.confirm(payment=payment, external_payment_id=f"charge-{order.pk}")
                order.refresh_from_db()
            else:
                OrderStateService.transition(order=order, to_status=status)
        return order

    def delivered_preview(self, order, *, usage=None):
        job = GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED,
            attempt=1,
            provider="openai",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            output_metadata={"model": "gpt-image-2", **({"usage": usage} if usage else {})},
        )
        return GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=f"generated/order-{order.pk}/preview-{job.pk}.png",
            metadata={"internal_approved": True, "deliveries": [{"status": "sent"}]},
        )

    def to_preview_review(self, order, *, usage=None):
        self.walk(
            order,
            Order.Status.AWAITING_PHOTOS,
            Order.Status.READY_FOR_CHECKOUT,
            Order.Status.PAID,
            Order.Status.PREVIEW_GENERATING,
            Order.Status.INTERNAL_PREVIEW_REVIEW,
        )
        asset = self.delivered_preview(order, usage=usage)
        OrderStateService.transition(order=order, to_status=Order.Status.PREVIEW_REVIEW)
        return asset


class OrderEventEmissionTests(MetricsFixtureMixin, TestCase):
    def test_every_transition_is_logged_with_from_and_to(self):
        order = self.order()
        self.walk(order, Order.Status.AWAITING_PHOTOS, Order.Status.READY_FOR_CHECKOUT, Order.Status.PAID)

        events = list(order.events.order_by("created_at").values_list("event_type", "from_status", "to_status", "actor_kind"))
        self.assertEqual(
            events,
            [
                ("order.status_changed", "draft", "awaiting_photos", "system"),
                ("order.status_changed", "awaiting_photos", "ready_for_checkout", "system"),
                ("order.status_changed", "ready_for_checkout", "awaiting_payment", "system"),
                ("order.status_changed", "awaiting_payment", "paid", "system"),
            ],
        )

    def test_rejected_transition_logs_nothing(self):
        order = self.order()
        with self.assertRaises(Exception):
            OrderStateService.transition(order=order, to_status=Order.Status.PAID)
        self.assertEqual(order.events.count(), 0)

    def test_event_insert_failure_rolls_back_the_status_change(self):
        """Status write and event insert are one unit even in autocommit
        callers (console views, preview delivery): a failed INSERT must not
        leave a changed status without a log entry."""
        order = self.order()
        with mock.patch(
            "apps.core.services.order_state.OrderEvent.objects.create",
            side_effect=RuntimeError("event insert failed"),
        ):
            with self.assertRaises(RuntimeError):
                OrderStateService.transition(order=order, to_status=Order.Status.AWAITING_PHOTOS)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DRAFT)
        self.assertEqual(OrderEvent.objects.count(), 0)

    def test_customer_approval_is_logged_once(self):
        order = self.order()
        asset = self.to_preview_review(order)
        PreviewFeedbackService.approve(order=order)
        PreviewFeedbackService.approve(order=order)

        approvals = order.events.filter(event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED)
        self.assertEqual(approvals.count(), 1)
        self.assertEqual(approvals.get().actor_kind, OrderEvent.Actor.CUSTOMER)
        self.assertEqual(approvals.get().payload, {"asset_id": asset.pk})

    def test_revision_request_is_visible_through_status_log_and_revision_row(self):
        order = self.order()
        self.to_preview_review(order)
        PreviewFeedbackService.request_revision(order=order, category=Revision.Category.FACE)
        self.assertTrue(order.events.filter(to_status=Order.Status.REVISION_REQUESTED).exists())
        self.assertEqual(order.events.filter(event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED).count(), 0)


class ProviderUsageCaptureTests(TestCase):
    def _provider(self, response):
        client = mock.Mock()
        client.images.edit.return_value = response
        return OpenAIImageProvider(client=client, model="gpt-image-2", proxy_pool=None)

    def test_usage_tokens_are_kept_as_cost_evidence(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json="aGk=")],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=4160, total_tokens=5360, extra="x"),
        )
        result = self._provider(response)._generate(self._provider(response).client, [], "prompt")
        self.assertEqual(result.metadata["model"], "gpt-image-2")
        self.assertEqual(result.metadata["usage"], {"input_tokens": 1200, "output_tokens": 4160, "total_tokens": 5360})

    def test_missing_usage_leaves_metadata_unchanged(self):
        response = SimpleNamespace(data=[SimpleNamespace(b64_json="aGk=")])
        provider = self._provider(response)
        result = provider._generate(provider.client, [], "prompt")
        self.assertEqual(result.metadata, {"model": "gpt-image-2"})


class ManualWorkLogAdminTests(MetricsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_superuser(username="operator", email="op@example.test", password="x")
        self.client.force_login(self.staff)
        self.target = self.order(Order.Status.PREVIEW_REVIEW)

    def test_operator_logs_explicit_minutes(self):
        response = self.client.post(
            reverse("admin:core_manualworklog_add"),
            {"order": self.target.pk, "minutes": 12, "activity": "preview_review", "note": "checked likeness"},
        )
        self.assertEqual(response.status_code, 302)

        event = OrderEvent.objects.get(event_type=OrderEvent.Type.MANUAL_WORK_LOGGED)
        self.assertEqual(event.order, self.target)
        self.assertEqual(event.actor_kind, OrderEvent.Actor.OPERATOR)
        self.assertEqual(event.actor_ref, "operator")
        self.assertEqual(event.payload, {"minutes": 12, "activity": "preview_review", "note": "checked likeness"})
        self.assertEqual(ManualWorkLog.objects.count(), 1)

    def test_invalid_minutes_are_rejected(self):
        for minutes in (0, -5, "abc", 24 * 60 + 1):
            with self.subTest(minutes=minutes):
                response = self.client.post(
                    reverse("admin:core_manualworklog_add"),
                    {"order": self.target.pk, "minutes": minutes, "activity": "qc"},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(OrderEvent.objects.filter(event_type=OrderEvent.Type.MANUAL_WORK_LOGGED).count(), 0)

    def test_log_is_append_only_in_admin(self):
        event = ManualWorkLog.objects.create(
            order=self.target, event_type=OrderEvent.Type.MANUAL_WORK_LOGGED, payload={"minutes": 3, "activity": "qc"}
        )
        # View-only: the change page renders read-only, but no edit can be submitted.
        self.assertEqual(self.client.get(reverse("admin:core_manualworklog_change", args=[event.pk])).status_code, 200)
        edit = self.client.post(
            reverse("admin:core_manualworklog_change", args=[event.pk]),
            {"order": self.target.pk, "minutes": 99, "activity": "qc", "note": ""},
        )
        self.assertEqual(edit.status_code, 403)
        event.refresh_from_db()
        self.assertEqual(event.payload["minutes"], 3)
        self.assertEqual(self.client.post(reverse("admin:core_manualworklog_delete", args=[event.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:core_orderevent_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:core_orderevent_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_manualworklog_changelist")).status_code, 200)


@override_settings(PILOT_IMAGE_CALL_COST_RUB=12.5)
class PilotMetricsSnapshotTests(MetricsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        # A: paid, preview delivered, customer approved, 2 provider calls (1 with usage), 12 manual minutes.
        self.approved = self.order()
        self.to_preview_review(self.approved, usage={"input_tokens": 100, "output_tokens": 400, "total_tokens": 500})
        GenerationJob.objects.create(
            order=self.approved, task_type=GenerationJob.TaskType.PREVIEW, status=GenerationJob.Status.FAILED,
            attempt=2, provider="openai", started_at=timezone.now(), finished_at=timezone.now(), error="boom",
        )
        PreviewFeedbackService.approve(order=self.approved)
        ManualWorkLog.objects.create(order=self.approved, event_type=OrderEvent.Type.MANUAL_WORK_LOGGED, payload={"minutes": 12, "activity": "preview_review"})
        # B: paid, preview delivered, revision requested, no manual log.
        self.revised = self.order()
        self.to_preview_review(self.revised)
        PreviewFeedbackService.request_revision(order=self.revised, category=Revision.Category.HAIR)
        # C: dropped out before checkout.
        self.cancelled = self.order()
        self.walk(self.cancelled, Order.Status.AWAITING_PHOTOS, Order.Status.CANCELLED)
        # D: created before the event log existed (no events at all).
        self.legacy = self.order(Order.Status.AWAITING_PAYMENT)
        # E: identity that never created an order.
        self.identity()

    def test_funnel_counts_ever_reached_including_pre_log_orders(self):
        funnel = PilotMetricsService().snapshot()["funnel"]
        self.assertEqual(funnel["identities"], 5)
        self.assertEqual(funnel["identities_without_orders"], 1)
        self.assertEqual(funnel["orders_created"], 4)
        self.assertEqual(funnel["reached"]["awaiting_photos"], 4)
        self.assertEqual(funnel["reached"]["ready_for_checkout"], 3)
        self.assertEqual(funnel["reached"]["awaiting_payment"], 3)
        self.assertEqual(funnel["reached"]["paid"], 2)
        self.assertEqual(funnel["reached"]["preview_review"], 2)
        self.assertEqual(funnel["reached"]["pack_generating"], 0)
        self.assertEqual(funnel["cancelled"], 1)
        self.assertEqual(funnel["current_status"]["revision_requested"], 1)

    def test_drop_off_reports_stage_left_and_open_orders(self):
        drop = PilotMetricsService().snapshot()["drop_off"]
        self.assertEqual(drop["terminal_from_status"], {"awaiting_photos->cancelled": 1})
        self.assertEqual(drop["terminal_without_log"], 0)
        self.assertEqual(
            drop["open_orders_by_current_status"],
            {"preview_review": 1, "revision_requested": 1, "awaiting_payment": 1},
        )

    def test_legacy_terminal_order_without_log_is_counted_as_unknown_drop_off(self):
        self.order(Order.Status.FAILED)
        drop = PilotMetricsService().snapshot()["drop_off"]
        self.assertEqual(drop["terminal_without_log"], 1)

    def test_payments_come_from_confirmed_payment_rows(self):
        payments = PilotMetricsService().snapshot()["payments"]
        self.assertEqual(payments["orders_paid"], 2)
        self.assertEqual(payments["by_provider_currency"], {"telegram_stars/XTR": {"count": 2, "amount_minor": 920}})

    def test_previews_approval_and_revision_rates(self):
        snapshot = PilotMetricsService().snapshot()
        self.assertEqual(snapshot["previews"]["preview_jobs"], 3)
        self.assertEqual(snapshot["previews"]["preview_jobs_succeeded"], 2)
        self.assertEqual(snapshot["previews"]["preview_jobs_failed"], 1)
        self.assertEqual(snapshot["previews"]["orders_with_preview"], 2)
        approval = snapshot["approval"]
        self.assertEqual(approval["orders_reached_preview_review"], 2)
        self.assertEqual(approval["orders_customer_approved"], 1)
        self.assertEqual(approval["orders_revision_requested"], 1)
        self.assertEqual(approval["approval_rate"], 0.5)
        self.assertEqual(approval["revision_rate"], 0.5)
        self.assertEqual(approval["revision_by_category"], {"hair": 1})

    def test_generation_cost_counts_provider_calls_and_known_tokens(self):
        cost = PilotMetricsService().snapshot()["generation_cost"]
        self.assertEqual(cost["provider_calls"], 3)
        self.assertEqual(cost["provider_calls_by_task_type"], {"preview": 3})
        self.assertEqual(cost["jobs_with_usage"], 1)
        self.assertEqual(cost["tokens"], {"input_tokens": 100, "output_tokens": 400, "total_tokens": 500})
        self.assertEqual(cost["calls_per_paid_order"], 1.5)

    def test_generation_cost_never_prices_history_with_todays_tariff(self):
        # DRF-2111: the fixture jobs were created without a cost snapshot →
        # UNKNOWN, not 3 × 12.5 ₽; no USD estimate exists any more.
        cost = PilotMetricsService().snapshot()["generation_cost"]
        self.assertNotIn("estimated_usd", cost)
        self.assertNotIn("unit_cost_usd", cost)
        self.assertEqual(cost["known_cost_minor"], 0)
        self.assertEqual(cost["known_cost_currency"], "RUB")
        self.assertIsNone(cost["known_cost_minor_per_paid_order"])
        self.assertEqual(cost["cost"]["unknown_price_count"], 3)
        self.assertEqual(cost["cost"]["possibly_billable_count"], 3)
        self.assertEqual(cost["cost"]["known_count"], 0)

    def test_manual_minutes_come_only_from_explicit_logs(self):
        # B has been sitting in revision_requested for "hours" but nobody logged work on it.
        Order.objects.filter(pk=self.revised.pk).update(updated_at=timezone.now() - timedelta(hours=6))
        manual = PilotMetricsService().snapshot()["manual_work"]
        self.assertEqual(manual["entries"], 1)
        self.assertEqual(manual["orders_with_logs"], 1)
        self.assertEqual(manual["total_minutes"], 12)
        self.assertEqual(manual["minutes_per_logged_order"], 12.0)
        # the unlogged paid order has UNKNOWN minutes: it is counted, not averaged in as 0
        self.assertEqual(manual["paid_orders_with_logs"], 1)
        self.assertEqual(manual["minutes_per_paid_order"], 12.0)
        self.assertEqual(manual["paid_orders_without_logs"], 1)
        self.assertEqual(manual["minutes_by_activity"], {"preview_review": 12})

    def test_delivery_counts_ready_for_delivery_and_delivered(self):
        delivered = self.order()
        self.to_preview_review(delivered)
        self.walk(
            delivered,
            Order.Status.PACK_GENERATING,
            Order.Status.QUALITY_CONTROL,
            Order.Status.READY_FOR_DELIVERY,
            Order.Status.DELIVERY_IN_PROGRESS,
            Order.Status.DELIVERED,
        )
        waiting = self.order()
        self.to_preview_review(waiting)
        self.walk(waiting, Order.Status.PACK_GENERATING, Order.Status.QUALITY_CONTROL, Order.Status.READY_FOR_DELIVERY)

        snapshot = PilotMetricsService().snapshot()

        self.assertEqual(snapshot["delivery"], {"orders_delivered": 1, "orders_ready_for_delivery": 2})
        self.assertEqual(snapshot["funnel"]["reached"][Order.Status.DELIVERED], 1)
        self.assertEqual(snapshot["funnel"]["reached"][Order.Status.READY_FOR_DELIVERY], 2)
        self.assertNotIn("delivered_status_defined", snapshot["delivery"])

    def test_delivered_is_inferred_for_orders_that_predate_the_log(self):
        # An order that reached DELIVERED before the event log existed still
        # counts for every earlier happy-path stage.
        before = PilotMetricsService().snapshot()["funnel"]["reached"]
        self.order(status=Order.Status.DELIVERED)

        after = PilotMetricsService().snapshot()["funnel"]["reached"]

        for stage in (Order.Status.PAID, Order.Status.READY_FOR_DELIVERY, Order.Status.DELIVERED):
            self.assertEqual(after[stage], before[stage] + 1, stage)

    def test_window_filters_by_order_creation(self):
        Order.objects.filter(pk=self.cancelled.pk).update(created_at=timezone.now() - timedelta(days=30))
        since = timezone.now() - timedelta(days=1)
        snapshot = PilotMetricsService(since=since).snapshot()
        self.assertEqual(snapshot["funnel"]["orders_created"], 3)
        self.assertEqual(snapshot["funnel"]["cancelled"], 0)
        self.assertEqual(snapshot["window"]["since"], since.isoformat())


class PilotMetricsCommandTests(MetricsFixtureMixin, TestCase):
    def test_json_output_has_all_sections(self):
        self.walk(self.order(), Order.Status.AWAITING_PHOTOS)
        out = StringIO()
        call_command("pilot_metrics", "--json", stdout=out)
        data = json.loads(out.getvalue())
        self.assertEqual(
            set(data),
            {"window", "funnel", "drop_off", "payments", "previews", "approval", "generation_cost", "manual_work", "delivery"},
        )
        self.assertEqual(data["funnel"]["orders_created"], 1)

    def test_text_output_and_since_filter(self):
        old = self.order()
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        self.order()
        out = StringIO()
        call_command("pilot_metrics", "--since", (timezone.now() - timedelta(days=2)).strftime("%Y-%m-%d"), stdout=out)
        text = out.getvalue()
        self.assertIn("funnel", text)
        self.assertIn("orders_created: 1", text)

    def test_invalid_date_is_rejected(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("pilot_metrics", "--since", "yesterday", stdout=StringIO())


class FullGenerationUsageMetricsTests(_full_generation.FullProductionTestCase):
    """DRF-2051 compat: FULL jobs keep provider metadata (model, usage) in
    output_metadata like preview jobs do, so pilot metrics can price them."""

    def test_full_usage_is_visible_in_generation_cost(self):
        usage = {"input_tokens": 70, "output_tokens": 330, "total_tokens": 400}

        class UsageProvider(_full_generation.FakeProvider):
            def generate_preview(self, request):
                result = super().generate_preview(request)
                return ImageGenerationResult(
                    content=result.content, metadata={"model": "gpt-image-2", "usage": usage}
                )

        order, _preview = self._make_order(config=_full_generation.SINGLE_CONFIG, emotions=("hello",))
        self._service(UsageProvider()).start(order=order)

        job = self._full_jobs(order).get()
        self.assertEqual(job.status, GenerationJob.Status.SUCCEEDED)
        self.assertEqual(job.output_metadata["asset_id"], self._final_assets(order).get().pk)
        self.assertEqual(job.output_metadata["model"], "gpt-image-2")
        self.assertEqual(job.output_metadata["usage"], usage)

        cost = PilotMetricsService().snapshot()["generation_cost"]
        self.assertEqual(cost["provider_calls_by_task_type"].get("full"), 1)
        self.assertGreaterEqual(cost["jobs_with_usage"], 1)
        for key, value in usage.items():
            self.assertGreaterEqual(cost["tokens"].get(key, 0), value)

