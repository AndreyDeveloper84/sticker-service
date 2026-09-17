"""DRF-2051: full generation contract for 1 or N emotions.

After customer preview approval the production run must yield exactly the
purchased volume (single = 1, pack = N final assets), keyed by stable
emotion-code slots, idempotent on re-entry, with selective retry of
defective slots only and no blind retry after ambiguous provider
timeouts.
"""

import tempfile
from io import BytesIO
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.image_providers import ImageGenerationResult, classify_provider_failure
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    OrderPhoto,
    Payment,
    Product,
    Style,
    User,
)
from apps.core.services.full_production import FullProductionError, FullProductionService
from apps.core.storage import LocalMediaStorage

EMOTIONS3 = [
    {"code": "hello", "label": "Привет"},
    {"code": "bye", "label": "Пока"},
    {"code": "thanks", "label": "Спасибо"},
]

PACK3_CONFIG = {
    "kind": "pack",
    "quantity": 3,
    "emotion_count": 3,
    "emotions": EMOTIONS3,
    "price_minor": 50000,
    "currency": "RUB",
    "generation_prompt": "Make a personalized sticker pack",
}

SINGLE_CONFIG = {
    "kind": "single",
    "quantity": 1,
    "emotion_count": 1,
    "emotions": EMOTIONS3,
    "price_minor": 10000,
    "currency": "RUB",
    "generation_prompt": "Make a personalized sticker",
}

PILOT9_CODES = [
    "hello",
    "bye",
    "thanks",
    "great",
    "no",
    "love",
    "laugh",
    "angry",
    "surprised",
]

PREVIEW_BYTES = b"approved-preview-bytes"
PHOTO_BYTES = b"person-photo"


class FakeProvider:
    name = "fake"

    def __init__(self, fail_slots=()):
        self.fail_slots = set(fail_slots)
        self.requests = []

    def generate_preview(self, request):
        self.requests.append(request)
        slot = request.metadata.get("slot_key")
        if slot in self.fail_slots:
            raise RuntimeError(f"provider error on {slot}")
        return ImageGenerationResult(content=f"final-{slot}".encode())


class ClassifyingProvider(FakeProvider):
    """Provider with the optional classify_failure capability.

    fail_classes maps slot_key -> failure class; the exception message
    starts with the class word so classify_failure can derive it.
    """

    name = "classifying"

    def __init__(self, fail_classes=None):
        self.fail_classes = dict(fail_classes or {})
        super().__init__(fail_slots=self.fail_classes.keys())

    def generate_preview(self, request):
        slot = request.metadata.get("slot_key")
        if slot in self.fail_classes:
            self.requests.append(request)
            raise RuntimeError(f"{self.fail_classes[slot]} failure on {slot}")
        return ImageGenerationResult(content=f"final-{slot}".encode())

    def classify_failure(self, exc):
        return str(exc).split(" ", 1)[0]


class FullProductionTestCase(TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self._override = override_settings(MEDIA_ROOT=Path(self._root.name))
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.storage = LocalMediaStorage()

        self.user = User.objects.create()
        self.identity = ChannelIdentity.objects.create(
            user=self.user,
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id="full-prod-user",
        )
        self.style = Style.objects.create(
            code="comic", name="Comic", config={"prompt": "Preserve likeness"}
        )
        self._product_seq = 0

    # ------------------------------------------------------------ fixture

    def _product(self, config):
        self._product_seq += 1
        return Product.objects.create(
            code=f"product-{self._product_seq}", name="Product", config=config
        )

    def _make_order(
        self,
        *,
        config=PACK3_CONFIG,
        emotions=("hello", "bye", "thanks"),
        status=Order.Status.PREVIEW_REVIEW,
        confirmed_payment=True,
        customer_approved=True,
        with_photo=True,
    ):
        order = Order.objects.create(
            user=self.user,
            channel_identity=self.identity,
            product=self._product(config),
            style=self.style,
            status=status,
            selection={"emotions": list(emotions)},
        )
        if with_photo:
            key = f"orders/{order.pk}/reference.jpg"
            self.storage.save(key, BytesIO(PHOTO_BYTES))
            OrderPhoto.objects.create(
                order=order,
                storage_key=key,
                original_filename="reference.jpg",
                mime_type="image/jpeg",
                size_bytes=len(PHOTO_BYTES),
                status=OrderPhoto.Status.ACCEPTED,
            )
        job = GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.PREVIEW,
            status=GenerationJob.Status.SUCCEEDED,
            attempt=1,
            provider="fake",
        )
        preview_key = f"generated/order-{order.pk}/preview/job-{job.pk}-source.png"
        self.storage.save(preview_key, BytesIO(PREVIEW_BYTES))
        metadata = {
            "internal_approved": True,
            "deliveries": [{"status": "sent", "channel": "telegram", "message_id": "1"}],
        }
        if customer_approved:
            metadata["customer_approved"] = True
        preview = GeneratedAsset.objects.create(
            order=order,
            job=job,
            kind=GeneratedAsset.Kind.PREVIEW,
            storage_key=preview_key,
            size_bytes=len(PREVIEW_BYTES),
            metadata=metadata,
        )
        if confirmed_payment:
            Payment.objects.create(
                order=order,
                provider="telegram_stars",
                status=Payment.Status.CONFIRMED,
                amount_minor=50000,
                currency="RUB",
                confirmed_at=timezone.now(),
            )
        return order, preview

    def _service(self, provider):
        return FullProductionService(provider=provider, storage=self.storage)

    def _full_jobs(self, order):
        return GenerationJob.objects.filter(
            order=order, task_type=GenerationJob.TaskType.FULL
        )

    def _final_assets(self, order):
        return order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)

    # ------------------------------------------------------------- tests

    def test_single_product_produces_exactly_one_final_asset(self):
        order, _preview = self._make_order(config=SINGLE_CONFIG, emotions=("hello",))
        plan = self._service(FakeProvider()).start(order=order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        assets = list(self._final_assets(order))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].slot_key, "hello")
        self.assertEqual(assets[0].metadata["emotion"], "hello")
        self.assertTrue(self.storage.exists(assets[0].storage_key))
        self.assertEqual([slot.status for slot in plan], ["succeeded"])
        self.assertEqual(plan[0].asset_id, assets[0].pk)

    def test_pack_produces_exactly_n_final_assets_in_selection_order(self):
        order, _preview = self._make_order()
        plan = self._service(FakeProvider()).start(order=order, max_slots=None)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self.assertEqual(self._final_assets(order).count(), 3)
        self.assertEqual([slot.slot_key for slot in plan], ["hello", "bye", "thanks"])
        self.assertEqual([slot.emotion for slot in plan], ["hello", "bye", "thanks"])
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))
        self.assertEqual(self._full_jobs(order).count(), 3)
        for asset in self._final_assets(order):
            self.assertTrue(self.storage.exists(asset.storage_key))

    def test_pilot_nine_emotion_slots_are_deterministic(self):
        config = {
            **PACK3_CONFIG,
            "quantity": 9,
            "emotion_count": 9,
            "emotions": [{"code": code, "label": code} for code in PILOT9_CODES],
        }
        order, _preview = self._make_order(config=config, emotions=PILOT9_CODES)
        self.assertEqual(self._service(FakeProvider()).expected_slots(order), PILOT9_CODES)
        plan = self._service(FakeProvider()).production_plan(order)
        self.assertEqual([slot.slot_key for slot in plan], PILOT9_CODES)
        self.assertTrue(all(slot.status == "pending" for slot in plan))

    def test_reentry_creates_no_duplicate_jobs_or_assets(self):
        order, _preview = self._make_order()
        # First entry: one slot fails, order stays in PACK_GENERATING.
        self._service(FakeProvider(fail_slots={"bye"})).start(order=order, max_slots=None)
        kept_assets = {asset.slot_key: asset.pk for asset in self._final_assets(order)}

        # Plain re-entry does NOT auto-retry failed slots (retry_failed scope).
        plan = self._service(FakeProvider()).start(order=order, max_slots=None)
        self.assertEqual(self._full_jobs(order).count(), 3)
        self.assertEqual(
            [slot.status for slot in plan], ["succeeded", "failed", "succeeded"]
        )

        # Selective retry completes the plan without touching succeeded slots.
        plan = self._service(FakeProvider()).retry_failed(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self.assertEqual(self._final_assets(order).count(), 3)
        self.assertEqual(self._full_jobs(order).count(), 4)
        for slot_key, asset_id in kept_assets.items():
            self.assertEqual(self._final_assets(order).get(slot_key=slot_key).pk, asset_id)
            self.assertEqual(self._full_jobs(order).filter(slot_key=slot_key).count(), 1)
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))

        # Once the order left production, start() is forbidden and a no-op.
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).start(order=order)
        self.assertEqual(self._full_jobs(order).count(), 4)
        self.assertEqual(self._final_assets(order).count(), 3)

    def test_one_slot_failure_keeps_order_in_pack_generating(self):
        order, _preview = self._make_order()
        plan = self._service(FakeProvider(fail_slots={"bye"})).start(order=order, max_slots=None)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(self._final_assets(order).count(), 2)
        bye_job = self._full_jobs(order).get(slot_key="bye")
        self.assertEqual(bye_job.status, GenerationJob.Status.FAILED)
        self.assertIn("provider error on bye", bye_job.error)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "failed")
        self.assertTrue(bye.retryable)  # FakeProvider has no classifier -> "unknown"
        self.assertEqual(
            [slot.status for slot in plan], ["succeeded", "failed", "succeeded"]
        )

    def test_retry_failed_regenerates_only_the_failed_slot(self):
        order, _preview = self._make_order()
        self._service(FakeProvider(fail_slots={"bye"})).start(order=order, max_slots=None)
        kept_assets = {
            asset.slot_key: asset.pk
            for asset in self._final_assets(order)
        }

        plan = self._service(FakeProvider()).retry_failed(order=order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self.assertEqual(self._final_assets(order).count(), 3)
        # Successful slots were preserved: same asset ids, no new attempts.
        for slot_key, asset_id in kept_assets.items():
            self.assertEqual(self._final_assets(order).get(slot_key=slot_key).pk, asset_id)
            self.assertEqual(self._full_jobs(order).filter(slot_key=slot_key).count(), 1)
        bye_jobs = list(self._full_jobs(order).filter(slot_key="bye").order_by("attempt"))
        self.assertEqual(len(bye_jobs), 2)
        self.assertGreater(bye_jobs[1].attempt, bye_jobs[0].attempt)
        self.assertEqual(bye_jobs[0].status, GenerationJob.Status.FAILED)
        self.assertEqual(bye_jobs[1].status, GenerationJob.Status.SUCCEEDED)
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))

    def test_identity_source_is_photos_first_preview_last(self):
        # DRF-2080: customer photos carry identity; the approved preview is
        # passed last as a style/character reference only.
        order, preview = self._make_order()
        provider = FakeProvider()
        self._service(provider).start(order=order, max_slots=None)

        self.assertEqual(len(provider.requests), 3)
        for job in self._full_jobs(order):
            self.assertEqual(job.input_metadata["source_preview_id"], preview.pk)
            self.assertEqual(job.input_metadata["slot_key"], job.slot_key)
        for request in provider.requests:
            slot = request.metadata["slot_key"]
            self.assertEqual(request.metadata["task_type"], GenerationJob.TaskType.FULL)
            self.assertNotIn(f"Emotion: {slot}", request.prompt)
            self.assertEqual(request.reference_images[0].content, PHOTO_BYTES)
            self.assertEqual(request.reference_images[-1].content, PREVIEW_BYTES)

    def test_start_is_forbidden_without_payment_and_approval(self):
        forbidden_statuses = [
            Order.Status.DRAFT,
            Order.Status.AWAITING_PAYMENT,
            Order.Status.PAID,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        ]
        for status in forbidden_statuses:
            with self.subTest(status=status):
                order, _preview = self._make_order(status=status)
                with self.assertRaises(FullProductionError):
                    self._service(FakeProvider()).start(order=order)
                order.refresh_from_db()
                self.assertEqual(order.status, status)
                self.assertFalse(self._full_jobs(order).exists())

    def test_start_requires_customer_approved_preview(self):
        order, _preview = self._make_order(customer_approved=False)
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).start(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertFalse(self._full_jobs(order).exists())

    def test_start_requires_confirmed_payment(self):
        order, _preview = self._make_order(confirmed_payment=False)
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).start(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertFalse(self._full_jobs(order).exists())

    def test_ambiguous_failure_is_failed_closed_and_never_auto_retried(self):
        order, _preview = self._make_order()
        plan = self._service(
            ClassifyingProvider({"bye": "ambiguous"})
        ).start(order=order, max_slots=None)

        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "failed")
        self.assertFalse(bye.retryable)
        bye_job = self._full_jobs(order).get(slot_key="bye")
        self.assertEqual(bye_job.output_metadata["failure_class"], "ambiguous")

        with self.assertRaises(FullProductionError) as ctx:
            self._service(FakeProvider()).retry_failed(order=order)
        self.assertIn("bye", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 1)
        self.assertFalse(self._final_assets(order).filter(slot_key="bye").exists())

    def test_retry_skips_ambiguous_slots_but_processes_retryable_ones(self):
        order, _preview = self._make_order()
        self._service(
            ClassifyingProvider({"bye": "ambiguous", "thanks": "api"})
        ).start(order=order, max_slots=None)

        with self.assertRaises(FullProductionError) as ctx:
            self._service(FakeProvider()).retry_failed(order=order)
        self.assertIn("bye", str(ctx.exception))

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        # Retryable "api" slot was regenerated; the ambiguous one was not.
        self.assertEqual(self._full_jobs(order).filter(slot_key="thanks").count(), 2)
        self.assertTrue(self._final_assets(order).filter(slot_key="thanks").exists())
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 1)

    def test_definitive_api_failure_is_retryable(self):
        order, _preview = self._make_order()
        plan = self._service(ClassifyingProvider({"bye": "api"})).start(order=order, max_slots=None)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertTrue(bye.retryable)

        plan = self._service(FakeProvider()).retry_failed(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))

    def test_provider_without_classify_failure_defaults_to_retryable(self):
        provider = FakeProvider(fail_slots={"bye"})
        self.assertFalse(hasattr(provider, "classify_failure"))
        order, _preview = self._make_order()
        plan = self._service(provider).start(order=order, max_slots=None)

        bye = next(slot for slot in plan if slot.slot_key == "bye")
        bye_job = self._full_jobs(order).get(slot_key="bye")
        self.assertEqual(bye_job.output_metadata["failure_class"], "unknown")
        self.assertTrue(bye.retryable)

    def test_expected_slots_rejects_selection_mismatch(self):
        order, _preview = self._make_order(emotions=("hello",))
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).expected_slots(order)

    def test_classify_provider_failure_helper(self):
        classifying = ClassifyingProvider({"hello": "ambiguous"})
        try:
            classifying.generate_preview(
                # minimal request stub: only metadata is read on the failure path
                type("Req", (), {"metadata": {"slot_key": "hello"}})()
            )
        except RuntimeError as exc:
            self.assertEqual(classify_provider_failure(classifying, exc), "ambiguous")
        self.assertEqual(
            classify_provider_failure(FakeProvider(), RuntimeError("x")), "unknown"
        )

    # ------------------------------------- F1: QC selective regeneration

    def _back_to_pack_generating(self, order):
        """Mirror DRF-2052 QC FAIL: order returns to production with retry slots."""
        order.status = Order.Status.PACK_GENERATING
        order.save(update_fields=["status"])

    def test_regenerate_slots_replaces_current_asset_and_keeps_audit(self):
        order, _preview = self._make_order()
        self._service(FakeProvider()).start(order=order, max_slots=None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self._back_to_pack_generating(order)
        old_bye = self._final_assets(order).get(slot_key="bye").pk

        plan = self._service(FakeProvider()).regenerate_slots(
            order=order, slot_keys=["bye"]
        )

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        bye_assets = list(
            self._final_assets(order).filter(slot_key="bye").order_by("created_at", "pk")
        )
        # Old asset retained for audit; the new one is current.
        self.assertEqual(len(bye_assets), 2)
        self.assertEqual(bye_assets[0].pk, old_bye)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "succeeded")
        self.assertEqual(bye.asset_id, bye_assets[1].pk)
        # Other slots got no new jobs.
        self.assertEqual(self._full_jobs(order).filter(slot_key="hello").count(), 1)
        self.assertEqual(self._full_jobs(order).filter(slot_key="thanks").count(), 1)
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 2)
        # production_plan reports the new asset as current.
        plan_after = self._service(FakeProvider()).production_plan(order)
        self.assertEqual(
            next(slot for slot in plan_after if slot.slot_key == "bye").asset_id,
            bye_assets[1].pk,
        )

    def test_failed_regeneration_keeps_order_in_pack_generating(self):
        order, _preview = self._make_order()
        self._service(FakeProvider()).start(order=order, max_slots=None)
        self._back_to_pack_generating(order)
        old_bye = self._final_assets(order).get(slot_key="bye").pk

        plan = self._service(FakeProvider(fail_slots={"bye"})).regenerate_slots(
            order=order, slot_keys=["bye"]
        )

        # No bounce back to QUALITY_CONTROL with the stale asset.
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "failed")
        self.assertEqual(bye.asset_id, old_bye)  # old asset still current

        plan = self._service(FakeProvider()).regenerate_slots(
            order=order, slot_keys=["bye"]
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "succeeded")
        self.assertNotEqual(bye.asset_id, old_bye)

    def test_regenerate_slots_validates_slot_keys(self):
        order, _preview = self._make_order()
        self._service(FakeProvider()).start(order=order, max_slots=None)
        self._back_to_pack_generating(order)
        with self.assertRaises(FullProductionError) as ctx:
            self._service(FakeProvider()).regenerate_slots(
                order=order, slot_keys=["bye", "nope"]
            )
        self.assertIn("nope", str(ctx.exception))
        self.assertEqual(self._full_jobs(order).count(), 3)

    def test_regenerate_slots_requires_pack_generating(self):
        order, _preview = self._make_order()  # PREVIEW_REVIEW
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).regenerate_slots(order=order, slot_keys=["hello"])
        self.assertFalse(self._full_jobs(order).exists())

        self._service(FakeProvider()).start(order=order, max_slots=None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        with self.assertRaises(FullProductionError):
            self._service(FakeProvider()).regenerate_slots(order=order, slot_keys=["hello"])
        self.assertEqual(self._full_jobs(order).count(), 3)

    def test_regenerate_slots_rejects_ambiguous_blocked_slot(self):
        order, _preview = self._make_order()
        self._service(ClassifyingProvider({"bye": "ambiguous"})).start(
            order=order, max_slots=None
        )
        with self.assertRaises(FullProductionError) as ctx:
            self._service(FakeProvider()).regenerate_slots(order=order, slot_keys=["bye"])
        self.assertIn("bye", str(ctx.exception))
        # No new attempt for the blocked slot.
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 1)

    # ------------------------------------- F2: no blind billable retry

    def test_stale_running_slot_is_failed_closed_not_blind_retried(self):
        order, preview = self._make_order()
        # Simulates a worker killed mid-generation (e.g. gunicorn timeout).
        GenerationJob.objects.create(
            order=order,
            task_type=GenerationJob.TaskType.FULL,
            status=GenerationJob.Status.RUNNING,
            attempt=1,
            slot_key="bye",
            provider="fake",
            input_metadata={"source_preview_id": preview.pk, "slot_key": "bye"},
            started_at=timezone.now(),
        )
        provider = FakeProvider()
        plan = self._service(provider).start(order=order, max_slots=None)

        bye_jobs = list(self._full_jobs(order).filter(slot_key="bye"))
        self.assertEqual(len(bye_jobs), 1)  # no new attempt on this pass
        self.assertEqual(bye_jobs[0].status, GenerationJob.Status.FAILED)
        self.assertEqual(bye_jobs[0].output_metadata["failure_class"], "ambiguous")
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "failed")
        self.assertFalse(bye.retryable)
        # The provider was never called for the stale slot.
        self.assertNotIn(
            "bye", [request.metadata["slot_key"] for request in provider.requests]
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)

    def test_force_retry_slot_unblocks_ambiguous_slot(self):
        order, _preview = self._make_order()
        self._service(ClassifyingProvider({"bye": "ambiguous"})).start(
            order=order, max_slots=None
        )
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 1)

        plan = self._service(FakeProvider()).force_retry_slot(order=order, slot_key="bye")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        bye = next(slot for slot in plan if slot.slot_key == "bye")
        self.assertEqual(bye.status, "succeeded")
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 2)
        self.assertTrue(self._final_assets(order).filter(slot_key="bye").exists())

    def test_force_retry_slot_requires_blocked_slot(self):
        order, _preview = self._make_order()
        self._service(FakeProvider(fail_slots={"bye"})).start(order=order, max_slots=None)
        # bye failed retryable ("unknown"), hello succeeded — neither is blocked.
        for slot_key in ("bye", "hello"):
            with self.subTest(slot_key=slot_key):
                with self.assertRaises(FullProductionError):
                    self._service(FakeProvider()).force_retry_slot(
                        order=order, slot_key=slot_key
                    )
        self.assertEqual(self._full_jobs(order).filter(slot_key="bye").count(), 1)
        self.assertEqual(self._full_jobs(order).filter(slot_key="hello").count(), 1)

    def test_max_slots_batches_one_slot_per_call_until_complete(self):
        order, _preview = self._make_order()
        provider = FakeProvider()
        service = self._service(provider)

        plan = service.start(order=order)  # default max_slots=1
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            [slot.status for slot in plan], ["succeeded", "pending", "pending"]
        )
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PACK_GENERATING)

        plan = service.start(order=order)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            [slot.status for slot in plan], ["succeeded", "succeeded", "pending"]
        )

        plan = service.start(order=order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.QUALITY_CONTROL)
        self.assertEqual(self._final_assets(order).count(), 3)
        self.assertTrue(all(slot.status == "succeeded" for slot in plan))
