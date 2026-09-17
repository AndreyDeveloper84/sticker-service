"""DRF-2053: final sticker set delivery to the order channel.

Delivery sends the CURRENT final asset of every expected slot (latest
SUCCEEDED FULL attempt, DRF-2051 rule) in Order.selection order, records
each slot outcome in a FinalDelivery run, never sends a slot twice across
runs, and reaches DELIVERED only once the whole set plus the final
"set is ready" message went out. Every send is behind the DRF-2052 QC
gate: no PASS, or a PASS that no longer covers the current set, means
zero sends.
"""

import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings

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
from apps.core.services.final_delivery import (
    FinalDeliveryError,
    FinalDeliveryService,
    classify_delivery_failure,
)
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.services.preview_delivery import DeliveryResult
from apps.core.services.qc import HUMAN_CRITERIA, QcService
from apps.core.storage import LocalMediaStorage

READY_FOR_DELIVERY = Order.Status.READY_FOR_DELIVERY

PILOT9 = ["hello", "bye", "thanks", "great", "no", "love", "laugh", "angry", "surprised"]
EMOTIONS = [{"code": code, "label": f"Label {code}"} for code in PILOT9]


def product_config(quantity):
    return {
        "kind": "pack" if quantity > 1 else "single",
        "quantity": quantity,
        "emotion_count": quantity,
        "emotions": EMOTIONS,
    }


class ChannelError(Exception):
    def __init__(self, status_code, text="channel error"):
        self.status_code = status_code
        super().__init__(text)


class FakeAdapter:
    """Records every send; fails the slots listed in fail_slots (once each
    unless permanent=True) and optionally the summary message."""

    channel = ChannelIdentity.Channel.TELEGRAM

    def __init__(self, fail_slots=(), fail_summary=False, status_code=0, permanent=False):
        self.fail_slots = set(fail_slots)
        self.fail_summary = fail_summary
        self.status_code = status_code
        self.permanent = permanent
        self.items = []
        self.summaries = []
        self._seq = 0

    def send_final_item(self, **kwargs):
        slot = kwargs["filename"].split("-", 2)[2].rsplit(".", 1)[0]
        if slot in self.fail_slots:
            if not self.permanent:
                self.fail_slots.discard(slot)
            raise ChannelError(self.status_code, f"send failed for {slot}")
        self._seq += 1
        self.items.append(kwargs)
        return DeliveryResult(message_id=f"msg-{self._seq}", metadata={"seq": self._seq})

    def send_final_summary(self, **kwargs):
        if self.fail_summary:
            self.fail_summary = False
            raise ChannelError(self.status_code, "summary failed")
        self._seq += 1
        self.summaries.append(kwargs)
        return DeliveryResult(message_id=f"msg-{self._seq}", metadata={})


class FinalDeliveryTestCase(TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        override = override_settings(MEDIA_ROOT=Path(self._root.name))
        override.enable()
        self.addCleanup(override.disable)
        self.storage = LocalMediaStorage()

        self.user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=self.user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="tg-user-1",
        )
        self.style = Style.objects.create(code="comic", name="Comic")
        self._seq = 0

    # ---------------------------------------------------------- fixture

    def make_order(self, *, quantity=3, emotions=None, status=READY_FOR_DELIVERY, identity=None):
        self._seq += 1
        product = Product.objects.create(
            code=f"product-{self._seq}", name="Product", config=product_config(quantity)
        )
        return Order.objects.create(
            user=self.user,
            channel_identity=identity or self.identity,
            product=product,
            style=self.style,
            status=status,
            selection={"emotions": list(emotions or PILOT9[:quantity])},
        )

    def add_final(self, order, slot, *, attempt, content=None, status=GenerationJob.Status.SUCCEEDED):
        job = GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
            status=status,
            attempt=attempt,
            slot_key=slot,
            provider="fake",
        )
        if status != GenerationJob.Status.SUCCEEDED:
            return job, None
        data = content if content is not None else f"final-{slot}-{attempt}".encode()
        key = f"generated/order-{order.pk}/final/slot-{slot}-job-{job.pk}.png"
        self.storage.save(key, BytesIO(data))
        asset = GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=GeneratedAsset.Kind.FINAL,
            slot_key=slot,
            storage_key=key,
            size_bytes=len(data),
        )
        return job, asset

    @staticmethod
    def pass_qc(order):
        """Persist a QC PASS (DRF-2052) covering the CURRENT final set.

        Fixture shortcut for the QC console flow: the report records the
        slot_keys / asset_ids exactly as QcService evaluates them, so the
        delivery gate accepts the set until any current asset changes.
        """
        attempt = (order.qc_reports.order_by("attempt").last().attempt + 1) if order.qc_reports.exists() else 1
        return QcReport.objects.create(
            order=order,
            attempt=attempt,
            status=QcReport.Status.PASSED,
            expected_count=QcService.expected_asset_count(order),
            slot_keys=QcService.current_slot_keys(order),
            asset_ids=QcService.current_asset_ids(order),
            human_checklist={name: {"passed": True} for name in HUMAN_CRITERIA},
        )

    def make_ready_order(
        self, *, quantity=3, emotions=None, status=READY_FOR_DELIVERY, qc_pass=True
    ):
        order = self.make_order(quantity=quantity, emotions=emotions, status=status)
        assets = {}
        for attempt, slot in enumerate(order.selection["emotions"], start=1):
            _job, assets[slot] = self.add_final(order, slot, attempt=attempt)
        if qc_pass:
            self.pass_qc(order)
        return order, assets

    def service(self, adapter=None):
        return FinalDeliveryService(adapter=adapter or FakeAdapter(), storage=self.storage)

    # ------------------------------------------------------------ tests

    def test_single_product_sends_exactly_one_item_and_summary(self):
        order, assets = self.make_ready_order(quantity=1)
        adapter = FakeAdapter()
        plan = self.service(adapter).deliver(order=order)

        self.assertEqual(len(adapter.items), 1)
        self.assertEqual(adapter.items[0]["content"], b"final-hello-1")
        self.assertEqual(adapter.items[0]["index"], 1)
        self.assertEqual(adapter.items[0]["total"], 1)
        self.assertEqual(adapter.items[0]["caption"], "Стикер 1/1 · Label hello")
        self.assertEqual(len(adapter.summaries), 1)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

        run = FinalDelivery.objects.get(order=order)
        self.assertEqual(run.attempt, 1)
        self.assertEqual(run.channel, "telegram")
        self.assertEqual(run.status, FinalDelivery.Status.SENT)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.results[0]["slot_key"], "hello")
        self.assertEqual(run.results[0]["asset_id"], assets["hello"].pk)
        self.assertEqual(run.results[0]["message_id"], "msg-1")
        self.assertEqual(run.summary["status"], "sent")

    def test_pack_of_nine_sends_in_selection_order(self):
        shuffled = list(reversed(PILOT9))
        order, _assets = self.make_ready_order(quantity=9, emotions=shuffled)
        adapter = FakeAdapter()
        plan = self.service(adapter).deliver(order=order)

        self.assertEqual(len(adapter.items), 9)
        sent_slots = [item["filename"].split("-", 2)[2].rsplit(".", 1)[0] for item in adapter.items]
        self.assertEqual(sent_slots, shuffled)
        self.assertEqual([item["index"] for item in adapter.items], list(range(1, 10)))
        self.assertTrue(all(item["total"] == 9 for item in adapter.items))
        self.assertEqual([slot.slot_key for slot in plan.slots], shuffled)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

    def test_incomplete_final_set_sends_nothing(self):
        order = self.make_order(quantity=3)
        self.add_final(order, "hello", attempt=1)
        self.add_final(order, "bye", attempt=2)
        self.pass_qc(order)  # a PASS over an incomplete set must still not deliver
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError) as ctx:
            self.service(adapter).deliver(order=order)
        self.assertIn("thanks", str(ctx.exception))
        self.assertEqual(adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_slot_whose_latest_attempt_failed_is_not_current(self):
        order, _assets = self.make_ready_order(quantity=3)
        # Slot regenerated after PASS and the attempt FAILED: the QC-passed
        # asset is still the DRF-2051 "current" one, but the slot is not
        # settled — delivery must not ship it.
        self.add_final(order, "bye", attempt=4, status=GenerationJob.Status.FAILED)
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError) as ctx:
            self.service(adapter).deliver(order=order)
        self.assertIn("not succeeded", str(ctx.exception))
        self.assertIn("bye", str(ctx.exception))
        self.assertEqual(adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_slot_with_running_latest_attempt_sends_nothing(self):
        order, _assets = self.make_ready_order(quantity=3)
        self.add_final(order, "thanks", attempt=4, status=GenerationJob.Status.RUNNING)
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.items, [])

    def test_quantity_mismatch_sends_nothing(self):
        order = self.make_order(quantity=3, emotions=["hello", "bye"])
        self.add_final(order, "hello", attempt=1)
        self.add_final(order, "bye", attempt=2)
        self.pass_qc(order)
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.items, [])

    # ------------------------------------------------------- QC gate

    def test_without_qc_pass_sends_nothing(self):
        order, _assets = self.make_ready_order(quantity=3, qc_pass=False)
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError) as ctx:
            self.service(adapter).deliver(order=order)
        self.assertIn("no passed QC report", str(ctx.exception))
        self.assertEqual(adapter.items, [])
        self.assertEqual(adapter.summaries, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_failed_qc_report_only_sends_nothing(self):
        order, _assets = self.make_ready_order(quantity=1, qc_pass=False)
        QcReport.objects.create(
            order=order,
            attempt=1,
            status=QcReport.Status.FAILED,
            expected_count=1,
            slot_keys=QcService.current_slot_keys(order),
            asset_ids=QcService.current_asset_ids(order),
            reason_codes=["crop"],
        )
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())

    def test_qc_pass_invalidated_by_regenerated_slot_sends_nothing(self):
        order, _assets = self.make_ready_order(quantity=3)
        # Same slot_key, new current asset after PASS: the PASS is stale.
        self.add_final(order, "bye", attempt=4, content=b"final-bye-regenerated")
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError) as ctx:
            self.service(adapter).deliver(order=order)
        self.assertIn("changed after QC PASS", str(ctx.exception))
        self.assertEqual(adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_resume_re_checks_qc_gate_before_sending(self):
        order, _assets = self.make_ready_order(quantity=3)
        adapter = FakeAdapter(fail_slots={"bye"})
        service = self.service(adapter)
        service.deliver(order=order)
        self.assertEqual(len(adapter.items), 2)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)

        # A slot changes mid-delivery: the remaining slot must not go out.
        self.add_final(order, "bye", attempt=4, content=b"final-bye-regenerated")
        with self.assertRaises(FinalDeliveryError) as ctx:
            service.resume(order=order)
        self.assertIn("changed after QC PASS", str(ctx.exception))
        self.assertEqual(len(adapter.items), 2)
        self.assertEqual(adapter.summaries, [])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)
        self.assertEqual(FinalDelivery.objects.filter(order=order).count(), 1)

    def test_gate_is_not_bypassed_by_adapter_channel(self):
        """The gate runs before the adapter is consulted for either channel."""
        max_identity = ChannelIdentity.objects.create(
            user=self.user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-2"
        )
        order = self.make_order(quantity=1, identity=max_identity)
        self.add_final(order, "hello", attempt=1)
        adapter = FakeAdapter()
        adapter.channel = ChannelIdentity.Channel.MAX
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.items, [])

    def test_uses_asset_of_latest_succeeded_attempt(self):
        order, assets = self.make_ready_order(quantity=3)
        _job, regenerated = self.add_final(order, "bye", attempt=4, content=b"final-bye-regenerated")
        self.pass_qc(order)  # QC repeated over the regenerated set
        adapter = FakeAdapter()
        self.service(adapter).deliver(order=order)

        by_slot = {
            item["filename"].split("-", 2)[2].rsplit(".", 1)[0]: item for item in adapter.items
        }
        self.assertEqual(by_slot["bye"]["content"], b"final-bye-regenerated")
        run = FinalDelivery.objects.get(order=order)
        recorded = {item["slot_key"]: item["asset_id"] for item in run.results}
        self.assertEqual(recorded["bye"], regenerated.pk)
        self.assertNotEqual(recorded["bye"], assets["bye"].pk)

    def test_partial_channel_failure_keeps_order_in_progress(self):
        order, assets = self.make_ready_order(quantity=3)
        adapter = FakeAdapter(fail_slots={"bye"}, status_code=0)
        plan = self.service(adapter).deliver(order=order)

        # Other slots are still sent; one failure does not abort the run.
        self.assertEqual(len(adapter.items), 2)
        self.assertEqual(adapter.summaries, [])
        self.assertFalse(plan.complete)
        states = {slot.slot_key: slot for slot in plan.slots}
        self.assertEqual(states["hello"].status, "sent")
        self.assertEqual(states["hello"].message_id, "msg-1")
        self.assertEqual(states["bye"].status, "failed")
        self.assertEqual(states["bye"].failure_class, "retryable")
        self.assertIn("send failed for bye", states["bye"].error)
        self.assertEqual(states["thanks"].status, "sent")
        self.assertEqual(states["thanks"].message_id, "msg-2")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)
        run = FinalDelivery.objects.get(order=order)
        self.assertEqual(run.status, FinalDelivery.Status.FAILED)
        sent = [item for item in run.results if item["status"] == "sent"]
        self.assertEqual({item["slot_key"] for item in sent}, {"hello", "thanks"})
        self.assertTrue(all(item["message_id"] for item in sent))
        self.assertEqual(
            {item["asset_id"] for item in sent}, {assets["hello"].pk, assets["thanks"].pk}
        )

    def test_resume_sends_only_unsent_slots_without_duplicates(self):
        order, _assets = self.make_ready_order(quantity=3)
        adapter = FakeAdapter(fail_slots={"bye"})
        service = self.service(adapter)
        service.deliver(order=order)
        self.assertEqual(len(adapter.items), 2)

        plan = service.resume(order=order)
        self.assertEqual(len(adapter.items), 3)
        resent = [item["filename"].split("-", 2)[2].rsplit(".", 1)[0] for item in adapter.items[2:]]
        self.assertEqual(resent, ["bye"])
        self.assertEqual(len(adapter.summaries), 1)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

        runs = list(FinalDelivery.objects.filter(order=order).order_by("attempt"))
        self.assertEqual([run.attempt for run in runs], [1, 2])
        self.assertEqual(runs[0].status, FinalDelivery.Status.FAILED)
        self.assertEqual(runs[1].status, FinalDelivery.Status.SENT)
        self.assertEqual([item["slot_key"] for item in runs[1].results], ["bye"])
        all_sent = [
            item["slot_key"] for run in runs for item in run.results if item["status"] == "sent"
        ]
        self.assertEqual(sorted(all_sent), ["bye", "hello", "thanks"])

    def test_deliver_from_in_progress_resumes_and_repeated_resume_is_noop(self):
        order, _assets = self.make_ready_order(quantity=3)
        adapter = FakeAdapter(fail_slots={"thanks"})
        service = self.service(adapter)
        service.deliver(order=order)
        plan = service.deliver(order=order)  # deliver() from DELIVERY_IN_PROGRESS == resume
        self.assertTrue(plan.complete)
        self.assertEqual(len(adapter.items), 3)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

        # DELIVERED is terminal: nothing more can be sent.
        with self.assertRaises(FinalDeliveryError):
            service.resume(order=order)
        with self.assertRaises(FinalDeliveryError):
            service.deliver(order=order)
        self.assertEqual(len(adapter.items), 3)
        self.assertEqual(len(adapter.summaries), 1)

    def test_max_items_limits_run_and_keeps_run_in_progress(self):
        order, _assets = self.make_ready_order(quantity=9)
        adapter = FakeAdapter()
        service = self.service(adapter)
        plan = service.deliver(order=order, max_items=4)
        self.assertEqual(len(adapter.items), 4)
        self.assertFalse(plan.complete)
        self.assertEqual(adapter.summaries, [])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)
        run = FinalDelivery.objects.get(order=order)
        self.assertEqual(run.status, FinalDelivery.Status.IN_PROGRESS)

        service.resume(order=order, max_items=4)
        self.assertEqual(len(adapter.items), 8)
        plan = service.resume(order=order, max_items=4)
        self.assertEqual(len(adapter.items), 9)
        self.assertTrue(plan.complete)
        # A limited run without failures continues in the same attempt row.
        self.assertEqual(FinalDelivery.objects.filter(order=order).count(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, FinalDelivery.Status.SENT)
        self.assertEqual(len(run.results), 9)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

    def test_summary_failure_blocks_delivered_until_resumed(self):
        order, _assets = self.make_ready_order(quantity=1)
        adapter = FakeAdapter(fail_summary=True)
        service = self.service(adapter)
        plan = service.deliver(order=order)
        self.assertEqual(len(adapter.items), 1)
        self.assertEqual(plan.summary_status, "failed")
        self.assertFalse(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)

        plan = service.resume(order=order)
        self.assertEqual(len(adapter.items), 1)  # sticker not re-sent
        self.assertEqual(len(adapter.summaries), 1)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

    def test_permanent_channel_rejection_is_classified(self):
        order, _assets = self.make_ready_order(quantity=1)
        adapter = FakeAdapter(fail_slots={"hello"}, status_code=403, permanent=True)
        plan = self.service(adapter).deliver(order=order)
        self.assertEqual(plan.slots[0].status, "failed")
        self.assertEqual(plan.slots[0].failure_class, "permanent")
        self.assertEqual(classify_delivery_failure(ChannelError(0)), "retryable")
        self.assertEqual(classify_delivery_failure(ChannelError(502)), "retryable")
        self.assertEqual(classify_delivery_failure(ChannelError(429)), "retryable")
        self.assertEqual(classify_delivery_failure(ChannelError(400)), "permanent")
        self.assertEqual(classify_delivery_failure(RuntimeError("x")), "retryable")

    def test_missing_asset_file_is_recorded_as_slot_failure(self):
        order, assets = self.make_ready_order(quantity=2)
        self.storage._path(assets["bye"].storage_key).unlink()
        adapter = FakeAdapter()
        plan = self.service(adapter).deliver(order=order)
        states = {slot.slot_key: slot for slot in plan.slots}
        self.assertEqual(states["hello"].status, "sent")
        self.assertEqual(states["bye"].status, "failed")
        self.assertIn("missing", states["bye"].error)
        self.assertEqual(len(adapter.items), 1)

    def test_wrong_channel_adapter_is_rejected_before_sending(self):
        max_identity = ChannelIdentity.objects.create(
            user=self.user, channel=ChannelIdentity.Channel.MAX, external_user_id="max-1"
        )
        order = self.make_order(quantity=1, identity=max_identity)
        self.add_final(order, "hello", attempt=1)
        adapter = FakeAdapter()  # telegram
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).deliver(order=order)
        self.assertEqual(adapter.items, [])
        self.assertFalse(FinalDelivery.objects.filter(order=order).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_wrong_status_is_rejected(self):
        for status in (
            Order.Status.QUALITY_CONTROL,
            Order.Status.PACK_GENERATING,
            Order.Status.PREVIEW_REVIEW,
            Order.Status.DELIVERED,
            Order.Status.FAILED,
        ):
            with self.subTest(status=status):
                order, _assets = self.make_ready_order(quantity=1, status=status)
                adapter = FakeAdapter()
                with self.assertRaises(FinalDeliveryError):
                    self.service(adapter).deliver(order=order)
                self.assertEqual(adapter.items, [])

    def test_resume_requires_started_delivery(self):
        order, _assets = self.make_ready_order(quantity=1)
        adapter = FakeAdapter()
        with self.assertRaises(FinalDeliveryError):
            self.service(adapter).resume(order=order)
        self.assertEqual(adapter.items, [])
        order.refresh_from_db()
        self.assertEqual(order.status, READY_FOR_DELIVERY)

    def test_delivery_plan_before_any_run(self):
        order, assets = self.make_ready_order(quantity=2)
        plan = self.service().delivery_plan(order)
        self.assertEqual(plan.attempts, 0)
        self.assertEqual(plan.summary_status, "pending")
        self.assertEqual(
            [(slot.slot_key, slot.status, slot.asset_id) for slot in plan.slots],
            [("hello", "pending", assets["hello"].pk), ("bye", "pending", assets["bye"].pk)],
        )


class DeliveryTransitionTests(TestCase):
    def test_delivery_transitions(self):
        self.assertEqual(
            OrderStateService.allowed_targets(READY_FOR_DELIVERY),
            {Order.Status.DELIVERY_IN_PROGRESS, Order.Status.FAILED},
        )
        self.assertEqual(
            OrderStateService.allowed_targets(Order.Status.DELIVERY_IN_PROGRESS),
            {Order.Status.DELIVERED, Order.Status.FAILED},
        )
        self.assertEqual(OrderStateService.allowed_targets(Order.Status.DELIVERED), set())

    def test_transition_keys_stay_unique_status_members(self):
        # READY_FOR_DELIVERY is defined once (DRF-2052); DRF-2053 must add
        # its targets to that entry, not shadow it with a duplicate key.
        keys = list(OrderStateService.transitions)
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsInstance(key, Order.Status)
        self.assertEqual(sum(1 for key in keys if key == READY_FOR_DELIVERY), 1)

    def _order(self, status):
        user = User.objects.create()
        identity = ChannelIdentity.objects.create(
            user=user, channel=ChannelIdentity.Channel.TELEGRAM, external_user_id="t-1"
        )
        product = Product.objects.create(code="p", name="P", config=product_config(1))
        style = Style.objects.create(code="s", name="S")
        return Order.objects.create(
            user=user, channel_identity=identity, product=product, style=style, status=status
        )

    def test_delivered_is_terminal(self):
        order = self._order(Order.Status.DELIVERED)
        for target in (
            Order.Status.DELIVERY_IN_PROGRESS,
            READY_FOR_DELIVERY,
            Order.Status.FAILED,
            Order.Status.CANCELLED,
        ):
            with self.assertRaises(InvalidOrderTransition):
                OrderStateService.transition(order=order, to_status=target)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

    def test_ready_for_delivery_enters_delivery(self):
        order = self._order(READY_FOR_DELIVERY)
        OrderStateService.transition(order=order, to_status=Order.Status.DELIVERY_IN_PROGRESS)
        OrderStateService.transition(order=order, to_status=Order.Status.DELIVERED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)

    def test_in_progress_cannot_return_to_ready_for_delivery(self):
        # Recovery is an operator resume from DELIVERY_IN_PROGRESS; there is
        # no transition back to the QC exit state.
        order = self._order(Order.Status.DELIVERY_IN_PROGRESS)
        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(order=order, to_status=READY_FOR_DELIVERY)
        with self.assertRaises(InvalidOrderTransition):
            OrderStateService.transition(order=order, to_status=Order.Status.QUALITY_CONTROL)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERY_IN_PROGRESS)
