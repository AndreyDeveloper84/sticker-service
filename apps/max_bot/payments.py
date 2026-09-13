from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.request import Request, urlopen

from django.db import transaction

from apps.core.models import ChannelIdentity, Order, Payment
from apps.core.services.payment import PaymentError, PaymentService


class MaxPaymentError(ValueError):
    pass


@dataclass(frozen=True)
class CheckoutSession:
    checkout_url: str
    provider_reference: str = ""


@dataclass(frozen=True)
class PaymentConfirmation:
    payment_id: int
    external_payment_id: str
    status: str
    metadata: dict


class ExternalPaymentProvider(Protocol):
    name: str

    def create_checkout(self, *, payment: Payment) -> CheckoutSession: ...

    def parse_webhook(self, *, body: bytes, signature: str) -> PaymentConfirmation: ...


class HttpExternalPaymentProvider:
    """Small provider gateway contract; vendor-specific logic stays outside OrderService."""

    name = "external"

    def __init__(self, *, create_url: str, api_token: str = "", webhook_secret: str = ""):
        self.create_url = create_url.strip()
        self.api_token = api_token.strip()
        self.webhook_secret = webhook_secret.encode("utf-8")

    @classmethod
    def from_env(cls) -> "HttpExternalPaymentProvider":
        return cls(
            create_url=os.getenv("MAX_PAYMENT_PROVIDER_CREATE_URL", ""),
            api_token=os.getenv("MAX_PAYMENT_PROVIDER_API_TOKEN", ""),
            webhook_secret=os.getenv("MAX_PAYMENT_PROVIDER_WEBHOOK_SECRET", ""),
        )

    def create_checkout(self, *, payment: Payment) -> CheckoutSession:
        if not self.create_url:
            raise MaxPaymentError("External payment provider is not configured")

        payload = {
            "payment_id": payment.pk,
            "order_id": payment.order_id,
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "channel": "max",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = Request(
            self.create_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        checkout_url = str(data.get("checkout_url") or "").strip()
        if not checkout_url.startswith("https://"):
            raise MaxPaymentError("Provider returned invalid checkout URL")
        return CheckoutSession(
            checkout_url=checkout_url,
            provider_reference=str(data.get("provider_reference") or ""),
        )

    def parse_webhook(self, *, body: bytes, signature: str) -> PaymentConfirmation:
        if not self.webhook_secret:
            raise MaxPaymentError("Payment webhook secret is not configured")
        expected = hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise MaxPaymentError("Invalid payment webhook signature")

        try:
            data = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise MaxPaymentError("Invalid payment webhook payload") from exc

        return PaymentConfirmation(
            payment_id=int(data["payment_id"]),
            external_payment_id=str(data.get("external_payment_id") or "").strip(),
            status=str(data.get("status") or "").strip().lower(),
            metadata=data.get("metadata") or {},
        )


class MaxExternalPaymentAdapter:
    def __init__(self, *, provider: ExternalPaymentProvider):
        self.provider = provider

    @transaction.atomic
    def create_checkout(self, *, identity: ChannelIdentity) -> tuple[Payment, CheckoutSession]:
        if identity.channel != ChannelIdentity.Channel.MAX:
            raise MaxPaymentError("MAX identity is required")

        order = (
            Order.objects.select_for_update()
            .select_related("product")
            .filter(
                channel_identity=identity,
                status__in=[Order.Status.READY_FOR_CHECKOUT, Order.Status.AWAITING_PAYMENT],
            )
            .order_by("-created_at")
            .first()
        )
        if order is None:
            raise MaxPaymentError("No order ready for payment")

        existing = (
            Payment.objects.filter(
                order=order,
                provider=self.provider.name,
                status=Payment.Status.PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        if existing:
            payment = existing
            metadata = payment.metadata or {}
            checkout_url = str(metadata.get("checkout_url") or "")
            if checkout_url:
                return payment, CheckoutSession(
                    checkout_url=checkout_url,
                    provider_reference=str(metadata.get("provider_reference") or ""),
                )
        else:
            config = order.product.config or {}
            amount_minor = int(config.get("price_minor") or 0)
            currency = str(config.get("currency") or "RUB").upper()
            if amount_minor <= 0:
                raise MaxPaymentError("Product price_minor is not configured")
            try:
                payment = PaymentService.create_pending(
                    order=order,
                    provider=self.provider.name,
                    amount_minor=amount_minor,
                    currency=currency,
                    metadata={"channel": "max"},
                )
            except PaymentError as exc:
                raise MaxPaymentError(str(exc)) from exc

        session = self.provider.create_checkout(payment=payment)
        metadata = dict(payment.metadata or {})
        metadata.update(
            {
                "checkout_url": session.checkout_url,
                "provider_reference": session.provider_reference,
            }
        )
        payment.metadata = metadata
        payment.save(update_fields=["metadata", "updated_at"])
        return payment, session

    def confirm_webhook(self, *, body: bytes, signature: str) -> Payment:
        confirmation = self.provider.parse_webhook(body=body, signature=signature)
        if confirmation.status != "succeeded":
            raise MaxPaymentError("Payment is not confirmed by provider")
        if not confirmation.external_payment_id:
            raise MaxPaymentError("Provider payment id is required")

        try:
            payment = Payment.objects.select_related("order").get(
                pk=confirmation.payment_id,
                provider=self.provider.name,
            )
        except Payment.DoesNotExist as exc:
            raise MaxPaymentError("Unknown payment") from exc

        try:
            return PaymentService.confirm(
                payment=payment,
                external_payment_id=confirmation.external_payment_id,
                metadata={"provider_webhook": confirmation.metadata},
            )
        except PaymentError as exc:
            raise MaxPaymentError(str(exc)) from exc
