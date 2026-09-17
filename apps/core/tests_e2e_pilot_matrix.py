"""DRF-2054: dual-channel E2E matrix for both Pilot products (engineering E2E).

Matrix — Pilot is NOT ready until every cell passes:

    T1  Telegram  sticker-pack-9   9 stickers  460 XTR (RUB price 500 unused on TG)
    T2  Telegram  single-sticker   1 sticker   100 XTR
    M1  MAX       sticker-pack-9   9 stickers  500 RUB via YooKassa
    M2  MAX       single-sticker   1 sticker   100 RUB via YooKassa

Every cell runs the full customer + operator chain on the real pilot
catalog (`seed_live_test`):

    start → product → style → emotion(s) → photos → consent (MAX) → checkout
    → payment → preview (operator) → internal approve + deliver (console)
    → customer approve | 1 revision → approve → full generation (console,
    one slot per request) → QC (console) → delivery gate → final delivery
    (DRF-2053) → DELIVERED.

Engineering vs live: only the external boundaries are faked — Telegram Bot
API client, MAX Bot API client, YooKassa provider, OpenAI image provider.
Webhook views, adapters, domain services, admin console views, database
and media storage are the real code paths. The live/staging run of the
same matrix is described in docs/DRF-2054_pilot_e2e_live_runbook.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.image_providers import ImageGenerationResult
from apps.core.management.commands.seed_live_test import PILOT_EMOTIONS
from apps.core.models import (
    ChannelIdentity,
    GeneratedAsset,
    GenerationJob,
    Order,
    Payment,
    Product,
    QcReport,
    Revision,
)
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.channel_order_flow import PILOT_CONSENT_VERSION, order_emotion_codes
from apps.core.services.full_production import FullProductionService
from apps.core.services.generation import GenerationService
from apps.core.services.preview_delivery import DeliveryResult
from apps.core.services.qc import AUTOMATED_CHECKS, HUMAN_CRITERIA, QcService
from apps.core.storage import LocalMediaStorage
from apps.core.tests_qc import make_image
from apps.max_bot.paid_notice import PAID_NOTICE_TEXT
from apps.max_bot.production_notice import PRODUCTION_NOTICE_TEXT
from apps.max_bot.payments import CheckoutSession, PaymentConfirmation
from apps.telegram_bot.paid_notice import PAID_NOTICE_TEXT as TELEGRAM_PAID_NOTICE_TEXT
from apps.telegram_bot.payments import TelegramStarsPaymentAdapter

from apps.core.services.final_delivery import FinalDeliveryError, FinalDeliveryService

from apps.core.services.pilot_metrics import PilotMetricsService


TELEGRAM_WEBHOOK = "/telegram/webhook/"
MAX_WEBHOOK = "/max/webhook/"
MAX_PAYMENT_WEBHOOK = "/max/payment/webhook/"

PACK = "sticker-pack-9"
SINGLE = "single-sticker"
STYLE = "comic"

# Owner-approved pilot contract (DRF-2057 / seed_live_test).
PRICES = {
    PACK: {"quantity": 9, "stars": 460, "rub_minor": 50000},
    SINGLE: {"quantity": 1, "stars": 100, "rub_minor": 10000},
}
ALL_EMOTIONS = [item["code"] for item in PILOT_EMOTIONS]
SINGLE_EMOTION = "laugh"

PREVIEW_PNG = make_image(size=(1024, 1024), mode="RGB")


# --------------------------------------------------------------- fakes


class FakeImageProvider:
    """OpenAI stand-in: preview/revision return a plain PNG, FULL slots
    return QC-valid stickers (512x512 RGBA PNG). Records every request."""

    name = "e2e-fake-openai"

    def __init__(self):
        self.requests = []
        self.usage = {"input_tokens": 10, "output_tokens": 20}

    def generate_preview(self, request):
        task = request.metadata.get("task_type") or "preview"
        self.requests.append(
            {
                "task_type": task,
                "slot_key": request.metadata.get("slot_key"),
                "references": [ref.filename for ref in request.reference_images],
            }
        )
        content = make_image() if task == GenerationJob.TaskType.FULL else PREVIEW_PNG
        return ImageGenerationResult(
            content=content,
            mime_type="image/png",
            metadata={"model": self.name, "usage": dict(self.usage)},
        )


class FakeYooKassa:
    """YooKassa boundary: checkout creation + webhook confirmation object."""

    name = "yookassa"

    def __init__(self):
        self.checkouts = []

    def create_checkout(self, *, payment):
        self.checkouts.append(payment.pk)
        return CheckoutSession(
            checkout_url=f"https://pay.yookassa.test/{payment.pk}",
            provider_reference=f"yk-{payment.pk}",
        )

    def parse_webhook(self, *, body, signature=""):
        event = json.loads(body)
        obj = event["object"]
        return PaymentConfirmation(
            payment_id=int(obj["metadata"]["payment_id"]),
            external_payment_id=str(obj["id"]),
            status=str(obj.get("status") or "succeeded"),
            metadata={"event": event.get("event", "")},
            amount_minor=round(float(obj["amount"]["value"]) * 100),
            currency=str(obj["amount"]["currency"]).upper(),
        )


class FakeFinalDeliveryAdapter:
    """DRF-2053 channel transport stand-in; records every send in order."""

    def __init__(self, channel):
        self.channel = channel
        self.items = []
        self.summaries = []

    def send_final_item(self, *, recipient_id, content, mime_type, filename, caption, index, total):
        self.items.append(
            {
                "recipient_id": recipient_id,
                "filename": filename,
                "mime_type": mime_type,
                "size": len(content),
                "index": index,
                "total": total,
            }
        )
        return DeliveryResult(message_id=f"{self.channel}-final-{index}", metadata={})

    def send_final_summary(self, *, recipient_id, text, total):
        self.summaries.append({"recipient_id": recipient_id, "text": text, "total": total})
        return DeliveryResult(message_id=f"{self.channel}-summary", metadata={})


# ------------------------------------------------------------- drivers


class TelegramDriver:
    """Customer side of a Telegram order through the real webhook view."""

    channel = ChannelIdentity.Channel.TELEGRAM
    user = {"id": 550101, "first_name": "Anna", "username": "anna_pilot"}
    chat_id = 660101

    def __init__(self, test):
        self.test = test
        self.client = test.client
        self.bot = mock.MagicMock(name="TelegramBotClient")
        self.bot.get_file.return_value = {"file_path": "photos/source.jpg"}
        self.bot.download_file.return_value = b"customer-photo-bytes"
        self.bot.send_message.return_value = {"message_id": 1}
        self.bot.send_photo.return_value = {"message_id": 2, "chat": {"id": self.chat_id}}

    def _post(self, payload):
        with mock.patch("apps.telegram_bot.views.TelegramBotClient", return_value=self.bot):
            return self.client.post(
                TELEGRAM_WEBHOOK, data=json.dumps(payload), content_type="application/json"
            )

    def _message(self, **fields):
        return {"message": {"from": self.user, "chat": {"id": self.chat_id}, **fields}}

    def _callback(self, data, callback_id="cb"):
        return {
            "callback_query": {
                "id": callback_id,
                "from": self.user,
                "message": {"chat": {"id": self.chat_id}},
                "data": data,
            }
        }

    def recipient_id(self):
        return str(self.user["id"])

    def start_to_checkout(self, product_code, emotions):
        t = self.test
        t.assertEqual(self._post(self._message(text="/start")).status_code, 200)
        keyboard = self.bot.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"]
        t.assertEqual(
            [row[0]["callback_data"] for row in keyboard],
            [f"product:{PACK}", f"product:{SINGLE}"],
        )
        t.assertEqual(self._post(self._callback(f"product:{product_code}")).status_code, 200)
        t.assertEqual(self._post(self._callback(f"style:{product_code}:{STYLE}")).status_code, 200)
        order = Order.objects.get(channel_identity__external_user_id=self.recipient_id())
        t.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

        if product_code == PACK:
            t.assertEqual(self._post(self._callback("emotions:confirm")).status_code, 200)
        else:
            for code in emotions:
                t.assertEqual(self._post(self._callback(f"emotion:{code}")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order_emotion_codes(order), emotions)

        photo = self._message(photo=[{"file_id": "small"}, {"file_id": "big"}])
        t.assertEqual(self._post(photo).status_code, 200)
        # photos_done → consent screen only (parity with MAX): the order stays
        # in AWAITING_PHOTOS and no invoice/payment exists until accept.
        t.assertEqual(self._post(self._callback("photos_done", "cb-done")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        t.assertFalse(order.consent_accepted)
        consent = self.bot.send_message.call_args.kwargs
        t.assertIn("право использовать загруженные фотографии", consent["text"])
        t.assertEqual(
            consent["reply_markup"], {"inline_keyboard": [[{"text": "Принимаю", "callback_data": "consent:accept"}]]}
        )
        self.bot.send_invoice.assert_not_called()
        t.assertFalse(Payment.objects.filter(order=order).exists())
        # consent:accept → consent persisted → summary with the "Оплатить" button
        t.assertEqual(self._post(self._callback("consent:accept", "cb-consent")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order.status, Order.Status.READY_FOR_CHECKOUT)
        t.assertTrue(order.consent_accepted)
        t.assertEqual(order.consent_version, PILOT_CONSENT_VERSION)
        t.assertIsNotNone(order.consent_accepted_at)
        summary = self.bot.send_message.call_args.kwargs["text"]
        t.assertIn(f"Стикеров: {PRICES[product_code]['quantity']}", summary)
        t.assertIn(f"{PRICES[product_code]['stars']} Stars", summary)
        return order

    def pay(self, order):
        """pay → invoice → pre_checkout ok → successful_payment → PAID."""
        t = self.test
        t.assertEqual(self._post(self._callback("pay")).status_code, 200)
        invoice = self.bot.send_invoice.call_args.kwargs
        t.assertEqual(invoice["amount_stars"], PRICES[order.product.code]["stars"])
        payment = Payment.objects.get(order=order, provider="telegram_stars")
        t.assertEqual(TelegramStarsPaymentAdapter.payload(payment), invoice["payload"])
        t.assertEqual((payment.amount_minor, payment.currency), (invoice["amount_stars"], "XTR"))

        pre_checkout = {
            "pre_checkout_query": {
                "id": "pcq-1",
                "from": self.user,
                "currency": "XTR",
                "total_amount": invoice["amount_stars"],
                "invoice_payload": invoice["payload"],
            }
        }
        t.assertEqual(self._post(pre_checkout).status_code, 200)
        t.assertTrue(self.bot.answer_pre_checkout_query.call_args.kwargs["ok"])

        successful = self._message(
            successful_payment={
                "currency": "XTR",
                "total_amount": invoice["amount_stars"],
                "invoice_payload": invoice["payload"],
                "telegram_payment_charge_id": f"tg-charge-{order.pk}",
                "provider_payment_charge_id": f"provider-charge-{order.pk}",
            }
        )
        t.assertEqual(self._post(successful).status_code, 200)
        order.refresh_from_db()
        payment.refresh_from_db()
        t.assertEqual(order.status, Order.Status.PAID)
        t.assertEqual(payment.status, Payment.Status.CONFIRMED)
        # PAID confirmation exactly once (DRF-2077 follow-up): a redelivered
        # successful_payment update (Telegram retry) confirms idempotently and
        # sends nothing more.
        t.assertEqual(self.paid_notices(), 1)
        t.assertEqual(self._post(successful).status_code, 200)
        t.assertEqual(Payment.objects.filter(order=order, status=Payment.Status.CONFIRMED).count(), 1)
        t.assertEqual(self.paid_notices(), 1)
        return payment

    def paid_notices(self):
        return sum(
            1
            for call in self.bot.send_message.call_args_list
            if call.kwargs.get("text") == TELEGRAM_PAID_NOTICE_TEXT
            and call.kwargs.get("chat_id") == self.chat_id
        )

    def customer_approve(self):
        self.test.assertEqual(self._post(self._callback("preview_approve")).status_code, 200)

    def customer_revision(self, category):
        self.test.assertEqual(
            self._post(self._callback(f"preview_revision:{category}")).status_code, 200
        )

    def preview_delivery_patch(self):
        """Console preview delivery talks to the Telegram client at this boundary."""
        return mock.patch("apps.core.preview_delivery_console.TelegramBotClient", return_value=self.bot)

    def production_patch(self):
        """Start Full Production notifies MAX customers only; Telegram orders
        must never instantiate the MAX client (DRF-2075)."""
        return contextlib.nullcontext()

    def production_notices(self):
        return sum(
            1
            for call in self.bot.send_message.call_args_list
            if call.kwargs.get("text") == PRODUCTION_NOTICE_TEXT
        )

    def preview_controls_sent(self):
        return any(
            call.kwargs.get("text") == "Как вам превью?"
            for call in self.bot.send_message.call_args_list
        )


class MaxDriver:
    """Customer side of a MAX order through the real webhook views."""

    channel = ChannelIdentity.Channel.MAX
    user = {"user_id": 770202, "first_name": "Boris"}
    chat_id = 880202

    def __init__(self, test):
        self.test = test
        self.client = test.client
        self.bot = mock.MagicMock(name="MaxBotClient")
        self.bot.send_message.return_value = {"body": {"mid": "max-msg"}}
        self.bot.send_image.return_value = {"body": {"mid": "max-photo"}}
        self.yookassa = FakeYooKassa()

    def _post(self, payload):
        with mock.patch("apps.max_bot.views.MaxBotClient", return_value=self.bot), mock.patch(
            "apps.max_bot.checkout.YooKassaPaymentProvider.from_env", return_value=self.yookassa
        ), mock.patch("apps.max_bot.views.download_photo", return_value=b"customer-photo-bytes"):
            return self.client.post(MAX_WEBHOOK, data=json.dumps(payload), content_type="application/json")

    def _callback(self, payload, callback_id="cb"):
        return {
            "update_type": "message_callback",
            "callback": {"callback_id": callback_id, "payload": payload, "user": self.user},
            "message": {"recipient": {"chat_id": self.chat_id}, "body": {"mid": "mid-1"}},
        }

    def recipient_id(self):
        return str(self.user["user_id"])

    def start_to_checkout(self, product_code, emotions):
        t = self.test
        started = {"update_type": "bot_started", "chat_id": self.chat_id, "user": self.user}
        t.assertEqual(self._post(started).status_code, 200)
        buttons = self.bot.send_message.call_args.kwargs["buttons"]
        t.assertEqual([row[0]["payload"] for row in buttons], [f"product:{PACK}", f"product:{SINGLE}"])
        t.assertEqual(self._post(self._callback(f"product:{product_code}")).status_code, 200)
        t.assertEqual(self._post(self._callback(f"style:{product_code}:{STYLE}")).status_code, 200)
        order = Order.objects.get(channel_identity__external_user_id=self.recipient_id())
        t.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)

        if product_code == PACK:
            t.assertEqual(self._post(self._callback("emotions:confirm")).status_code, 200)
        else:
            for code in emotions:
                t.assertEqual(self._post(self._callback(f"emotion:{code}")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order_emotion_codes(order), emotions)

        photo = {
            "update_type": "message_created",
            "message": {
                "sender": self.user,
                "recipient": {"chat_id": self.chat_id},
                "body": {
                    "mid": "mid-photo",
                    "attachments": [{"type": "image", "payload": {"url": "https://cdn.max.test/p.jpg"}}],
                },
            },
        }
        t.assertEqual(self._post(photo).status_code, 200)
        # photos_done → consent screen only; the order stays in AWAITING_PHOTOS
        # and no checkout exists until the customer explicitly accepts.
        t.assertEqual(self._post(self._callback("photos_done", "cb-done")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order.status, Order.Status.AWAITING_PHOTOS)
        t.assertFalse(order.consent_accepted)
        consent = self.bot.send_message.call_args.kwargs
        t.assertIn("право использовать загруженные фотографии", consent["text"])
        t.assertEqual(consent["buttons"], [[{"text": "Принимаю", "payload": "consent:accept"}]])
        t.assertFalse(Payment.objects.filter(order=order).exists())
        # consent:accept → consent persisted → summary + real start_checkout()
        t.assertEqual(self._post(self._callback("consent:accept", "cb-consent")).status_code, 200)
        order.refresh_from_db()
        t.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        t.assertTrue(order.consent_accepted)
        t.assertEqual(order.consent_version, PILOT_CONSENT_VERSION)
        t.assertIsNotNone(order.consent_accepted_at)
        texts = [call.kwargs.get("text", "") for call in self.bot.send_message.call_args_list]
        t.assertTrue(any(f"Стикеров: {PRICES[product_code]['quantity']}" in text for text in texts))
        t.assertTrue(any(f"{PRICES[product_code]['rub_minor'] // 100} ₽" in text for text in texts))
        checkout = self.bot.send_message.call_args.kwargs
        t.assertEqual(checkout["chat_id"], str(self.chat_id))
        t.assertTrue(checkout["buttons"][0][0]["url"].startswith("https://pay.yookassa.test/"))
        return order

    def pay(self, order, *, amount_minor=None, external_id=None):
        """YooKassa webhook → PAID (amount verified against the pending payment)."""
        t = self.test
        payment = Payment.objects.get(order=order, provider="yookassa")
        t.assertEqual(
            (payment.amount_minor, payment.currency),
            (PRICES[order.product.code]["rub_minor"], "RUB"),
        )
        t.assertEqual(self.yookassa.checkouts, [payment.pk])
        amount = payment.amount_minor if amount_minor is None else amount_minor
        body = json.dumps(
            {
                "event": "payment.succeeded",
                "object": {
                    "id": external_id or f"yk-tx-{payment.pk}",
                    "status": "succeeded",
                    "amount": {"value": f"{amount / 100:.2f}", "currency": "RUB"},
                    "metadata": {"payment_id": payment.pk},
                },
            }
        )
        with mock.patch(
            "apps.max_bot.provider_webhook.YooKassaPaymentProvider.from_env", return_value=self.yookassa
        ), mock.patch("apps.max_bot.provider_webhook.MaxBotClient", return_value=self.bot):
            response = self.client.post(MAX_PAYMENT_WEBHOOK, data=body, content_type="application/json")
        order.refresh_from_db()
        payment.refresh_from_db()
        # PAID confirmation to the customer: exactly once per payment, only
        # after a CONFIRMED payment, never on a mismatch or a duplicate webhook.
        expected = 1 if payment.status == Payment.Status.CONFIRMED else 0
        t.assertEqual(self.paid_notices(), expected)
        return response, payment

    def paid_notices(self):
        return sum(
            1
            for call in self.bot.send_message.call_args_list
            if call.kwargs.get("text") == PAID_NOTICE_TEXT
            and call.kwargs.get("user_id") == self.recipient_id()
        )

    def customer_approve(self):
        self.test.assertEqual(self._post(self._callback("preview_approve")).status_code, 200)

    def customer_revision(self, category):
        self.test.assertEqual(
            self._post(self._callback(f"preview_revision:{category}")).status_code, 200
        )

    def preview_delivery_patch(self):
        return mock.patch("apps.core.preview_delivery_console.MaxBotClient", return_value=self.bot)

    def production_patch(self):
        """Start Full Production sends the "in production" notice through the
        MAX client at this boundary (DRF-2075) — never the real API in tests."""
        return mock.patch("apps.core.production_console.MaxBotClient", return_value=self.bot)

    def production_notices(self):
        return sum(
            1
            for call in self.bot.send_message.call_args_list
            if call.kwargs.get("text") == PRODUCTION_NOTICE_TEXT
            and call.kwargs.get("user_id") == self.recipient_id()
        )

    def preview_controls_sent(self):
        return any(
            call.kwargs.get("text") == "Как вам превью?"
            for call in self.bot.send_message.call_args_list
        )


# ---------------------------------------------------------- base case


class PilotE2ECase(TestCase):
    """Shared operator side (admin console) + assertions for every cell."""

    def setUp(self):
        call_command("seed_live_test", stdout=StringIO())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Secret enforcement is covered by the channel regression suites.
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name), TELEGRAM_WEBHOOK_SECRET="")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.storage = LocalMediaStorage()
        self.provider = FakeImageProvider()
        env = mock.patch.dict(os.environ, {"MAX_WEBHOOK_SECRET": "", "TELEGRAM_BOT_TOKEN": "", "MAX_BOT_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

        provider = self.provider
        storage = self.storage
        for name, factory in (
            ("get_generation_service", lambda self_: GenerationService(provider=provider, storage=storage)),
            ("get_full_production_service", lambda self_: FullProductionService(provider=provider, storage=storage)),
        ):
            patcher = mock.patch.object(ProductionOrderAdmin, name, factory)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.operator = get_user_model().objects.create_superuser(
            username="pilot-operator", email="operator@example.com", password="pass"
        )

    # -- console helpers ------------------------------------------------

    def _console(self, url_name, *args, data=None):
        self.client.force_login(self.operator)
        response = self.client.post(reverse(f"admin:{url_name}", args=args), data=data or {})
        self.assertEqual(response.status_code, 302, url_name)
        self.client.logout()
        return response

    def operator_generate_preview(self, order):
        self._console("core_order_generate_preview", order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
        asset = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by("-pk").first()
        self.assertIsNotNone(asset)
        self.assertEqual(asset.job.status, GenerationJob.Status.SUCCEEDED)
        return asset

    def operator_approve_and_deliver(self, driver, order, asset):
        self._console("core_order_approve_preview", order.pk, asset.pk)
        asset.refresh_from_db()
        self.assertTrue(asset.metadata.get("internal_approved"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.INTERNAL_PREVIEW_REVIEW, "approve alone must not start customer review")
        with driver.preview_delivery_patch():
            self._console("core_order_deliver_preview", order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        asset.refresh_from_db()
        deliveries = asset.metadata.get("deliveries") or []
        self.assertEqual([d["status"] for d in deliveries], ["sent"])
        self.assertEqual(deliveries[0]["channel"], driver.channel)
        self.assertTrue(driver.preview_controls_sent())

    def operator_full_production(self, driver, order, approved_preview):
        quantity = PRICES[order.product.code]["quantity"]
        emotions = order_emotion_codes(order)
        before = len(self.provider.requests)
        expected_notices = 1 if driver.channel == ChannelIdentity.Channel.MAX else 0
        self.assertEqual(driver.production_notices(), 0, "no production notice before production starts")
        for step in range(1, quantity + 1):
            with driver.production_patch():
                self._console("core_order_start_full_production", order.pk)
            order.refresh_from_db()
            expected = Order.Status.QUALITY_CONTROL if step == quantity else Order.Status.PACK_GENERATING
            self.assertEqual(order.status, expected, f"slot {step}/{quantity}")
            # DRF-2075: the MAX customer is told once when production starts;
            # slot batching re-entries never repeat it; Telegram gets nothing.
            self.assertEqual(driver.production_notices(), expected_notices, f"slot {step}/{quantity}")
        full_requests = [r for r in self.provider.requests[before:] if r["task_type"] == GenerationJob.TaskType.FULL]
        self.assertEqual([r["slot_key"] for r in full_requests], emotions)
        for request in full_requests:
            # identity lock: customer-approved preview is always the first reference
            self.assertEqual(request["references"][0], f"approved-preview-{approved_preview.pk}.png")
        jobs = GenerationJob.objects.filter(order=order, task_type=GenerationJob.TaskType.FULL)
        self.assertEqual(jobs.count(), quantity)
        self.assertTrue(all(job.status == GenerationJob.Status.SUCCEEDED for job in jobs))
        self.assertTrue(all(job.input_metadata["source_preview_id"] == approved_preview.pk for job in jobs))
        finals = QcService.current_final_assets(order)
        self.assertEqual(len(finals), quantity)
        self.assertEqual(sorted(a.slot_key for a in finals), sorted(emotions))
        # re-entry after completion is a rejected no-op, never a duplicate
        with driver.production_patch():
            self._console("core_order_start_full_production", order.pk)
        self.assertEqual(jobs.count(), quantity)
        self.assertEqual(driver.production_notices(), expected_notices)
        return finals

    def operator_qc_pass(self, order):
        quantity = PRICES[order.product.code]["quantity"]
        self._console("core_order_qc_start", order.pk)
        report = QcReport.objects.get(order=order, status=QcReport.Status.IN_PROGRESS)
        self.assertEqual(report.expected_count, quantity)
        self.assertEqual(len(report.asset_ids), quantity)
        checks = report.automated_checks
        self.assertTrue(checks["expected_count"])
        for slot in report.slot_keys:
            for name in AUTOMATED_CHECKS:
                if name != "expected_count":
                    self.assertTrue(checks[slot][name], f"{slot}:{name}")
        self._console("core_order_qc_finalize", order.pk, data={c: "on" for c in HUMAN_CRITERIA})
        report.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(report.status, QcReport.Status.PASSED)
        self.assertEqual(order.status, Order.Status.READY_FOR_DELIVERY)
        gate = QcService(storage=self.storage).assert_delivery_allowed(order=order)
        self.assertEqual(gate.pk, report.pk)
        return report

    def final_delivery(self, driver, order):
        """DRF-2053 stage: every cell must end DELIVERED, never silently skipped."""
        adapter = FakeFinalDeliveryAdapter(driver.channel)
        plan = FinalDeliveryService(adapter=adapter, storage=self.storage).deliver(order=order, max_items=None)
        self.assertTrue(plan.complete)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)
        emotions = order_emotion_codes(order)
        self.assertEqual([item["index"] for item in adapter.items], list(range(1, len(emotions) + 1)))
        self.assertEqual({item["recipient_id"] for item in adapter.items}, {driver.recipient_id()})
        self.assertEqual(len(adapter.summaries), 1)
        self.assertEqual([slot.status for slot in plan.slots], ["sent"] * len(emotions))
        self.assertEqual([slot.slot_key for slot in plan.slots], emotions)
        # DELIVERED is terminal: a repeated deliver is rejected, nothing is re-sent
        with self.assertRaises(FinalDeliveryError):
            FinalDeliveryService(adapter=adapter, storage=self.storage).deliver(order=order, max_items=None)
        self.assertEqual(len(adapter.items), len(emotions))
        self.assertEqual(len(adapter.summaries), 1)
        return plan

    # -- scenario ---------------------------------------------------------

    def run_cell(self, driver, product_code, *, revision=False):
        emotions = ALL_EMOTIONS if product_code == PACK else [SINGLE_EMOTION]
        order = driver.start_to_checkout(product_code, emotions)
        self.assertEqual(order.channel_identity.channel, driver.channel)

        if driver.channel == ChannelIdentity.Channel.TELEGRAM:
            payment = driver.pay(order)
        else:
            response, payment = driver.pay(order)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(Payment.objects.filter(order=order, status=Payment.Status.CONFIRMED).count(), 1)

        preview = self.operator_generate_preview(order)
        self.operator_approve_and_deliver(driver, order, preview)

        if revision:
            driver.customer_revision(Revision.Category.FACE)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.REVISION_REQUESTED)
            revision_row = Revision.objects.get(order=order)
            self.assertEqual(revision_row.source_preview_id, preview.pk)
            # No console action for revision generation exists in dev (gap
            # recorded in the DRF-2054 report); the service call is what an
            # operator runs from `manage.py shell` today.
            revised = GenerationService(provider=self.provider, storage=self.storage).generate_revision(order=order)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.INTERNAL_PREVIEW_REVIEW)
            self.assertEqual(revised.job.input_metadata["source_preview_id"], preview.pk)
            self.operator_approve_and_deliver(driver, order, revised)
            preview = revised

        driver.customer_approve()
        order.refresh_from_db()
        preview.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PREVIEW_REVIEW)
        self.assertTrue(preview.metadata.get("customer_approved"))
        # exactly one customer-approved preview drives production
        approved = [
            a for a in order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW)
            if (a.metadata or {}).get("customer_approved")
        ]
        self.assertEqual([a.pk for a in approved], [preview.pk])

        finals = self.operator_full_production(driver, order, preview)
        report = self.operator_qc_pass(order)
        self.assertEqual(report.asset_ids, [a.pk for a in finals])
        self.final_delivery(driver, order)
        return order


# ------------------------------------------------------------- matrix


class PilotMatrixTelegramTests(PilotE2ECase):
    def test_T1_telegram_pack_9x500(self):
        order = self.run_cell(TelegramDriver(self), PACK)
        self.assertEqual(order.product.code, PACK)
        self.assertEqual(Payment.objects.get(order=order).amount_minor, 460)

    def test_T2_telegram_single_1x100(self):
        order = self.run_cell(TelegramDriver(self), SINGLE)
        self.assertEqual(order_emotion_codes(order), [SINGLE_EMOTION])
        self.assertEqual(Payment.objects.get(order=order).amount_minor, 100)

    def test_T2_telegram_single_with_included_revision(self):
        order = self.run_cell(TelegramDriver(self), SINGLE, revision=True)
        self.assertEqual(Revision.objects.filter(order=order).count(), 1)
        self.assertEqual(order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).count(), 2)


class PilotMatrixMaxTests(PilotE2ECase):
    def test_M1_max_pack_9x500(self):
        order = self.run_cell(MaxDriver(self), PACK)
        payment = Payment.objects.get(order=order)
        self.assertEqual((payment.provider, payment.amount_minor, payment.currency), ("yookassa", 50000, "RUB"))

    def test_M2_max_single_1x100(self):
        order = self.run_cell(MaxDriver(self), SINGLE)
        payment = Payment.objects.get(order=order)
        self.assertEqual((payment.amount_minor, payment.currency), (10000, "RUB"))

    def test_M1_max_pack_with_included_revision(self):
        order = self.run_cell(MaxDriver(self), PACK, revision=True)
        self.assertEqual(Revision.objects.filter(order=order).count(), 1)


class PilotMatrixCrossChannelTests(PilotE2ECase):
    def test_both_channels_and_products_share_one_backend(self):
        """One DB, one console: the four cells coexist without interference."""
        tg = self.run_cell(TelegramDriver(self), PACK)
        mx = self.run_cell(MaxDriver(self), SINGLE)
        self.assertNotEqual(tg.user_id, mx.user_id)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(
            {o.channel_identity.channel for o in Order.objects.all()},
            {ChannelIdentity.Channel.TELEGRAM, ChannelIdentity.Channel.MAX},
        )
        self.assertEqual(GeneratedAsset.objects.filter(kind=GeneratedAsset.Kind.FINAL).count(), 10)
        self.assertEqual(Product.objects.filter(is_active=True).count(), 2)

        self.client.force_login(self.operator)
        response = self.client.get(reverse("admin:core_order_changelist"))
        self.assertContains(response, f"Order #{tg.pk}")
        self.assertContains(response, f"Order #{mx.pk}")

    def test_metrics_snapshot_sees_the_matrix(self):
        """DRF-2055 handoff: the snapshot counts what the matrix produced."""
        self.run_cell(TelegramDriver(self), SINGLE)
        snapshot = PilotMetricsService().snapshot()
        self.assertEqual(snapshot["payments"]["orders_paid"], 1)
        self.assertEqual(snapshot["previews"]["orders_with_preview"], 1)
        self.assertEqual(snapshot["approval"]["orders_customer_approved"], 1)
        self.assertEqual(snapshot["generation_cost"]["provider_calls_by_task_type"].get("full"), 1)
