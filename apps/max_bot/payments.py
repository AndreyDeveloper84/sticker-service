from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from django.db import transaction

from apps.core.models import ChannelIdentity, Order, Payment
from apps.core.services.payment import PaymentError, PaymentService


class MaxPaymentError(ValueError):
    pass


class MaxPaymentIgnored(MaxPaymentError):
    """Webhook is valid but needs no action; the provider must still be ACKed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    amount_minor: int | None = None
    currency: str = ""


class ExternalPaymentProvider(Protocol):
    name: str

    def create_checkout(self, *, payment: Payment) -> CheckoutSession: ...

    def parse_webhook(self, *, body: bytes, signature: str) -> PaymentConfirmation: ...


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
            raise MaxPaymentIgnored(f"status {confirmation.status}")
        if not confirmation.external_payment_id:
            raise MaxPaymentError("Provider payment id is required")

        try:
            payment = Payment.objects.select_related("order").get(
                pk=confirmation.payment_id,
                provider=self.provider.name,
            )
        except Payment.DoesNotExist as exc:
            raise MaxPaymentIgnored("unknown payment") from exc

        if (
            confirmation.amount_minor is not None
            and confirmation.amount_minor != payment.amount_minor
        ):
            raise MaxPaymentError("Provider payment amount mismatch")
        if confirmation.currency and confirmation.currency != payment.currency:
            raise MaxPaymentError("Provider payment currency mismatch")

        try:
            return PaymentService.confirm(
                payment=payment,
                external_payment_id=confirmation.external_payment_id,
                metadata={"provider_webhook": confirmation.metadata},
            )
        except PaymentError as exc:
            raise MaxPaymentError(str(exc)) from exc
