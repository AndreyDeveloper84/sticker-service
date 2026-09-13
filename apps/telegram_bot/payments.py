from __future__ import annotations

from dataclasses import dataclass

from apps.core.models import ChannelIdentity, Order, Payment
from apps.core.services.payment import PaymentError, PaymentService


class TelegramPaymentError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramStarsPaymentAdapter:
    provider = "telegram_stars"
    currency = "XTR"

    def payment_for_identity(self, identity: ChannelIdentity) -> Payment:
        order = (
            Order.objects.select_related("product", "channel_identity")
            .filter(
                channel_identity=identity,
                status__in=[Order.Status.READY_FOR_CHECKOUT, Order.Status.AWAITING_PAYMENT],
            )
            .order_by("-created_at")
            .first()
        )
        if order is None:
            raise TelegramPaymentError("No order is ready for payment")

        try:
            amount = int((order.product.config or {}).get("price_stars"))
        except (TypeError, ValueError):
            raise TelegramPaymentError("Product Telegram Stars price is not configured")
        if amount <= 0:
            raise TelegramPaymentError("Product Telegram Stars price is not configured")

        existing = (
            Payment.objects.filter(
                order=order,
                provider=self.provider,
                status=Payment.Status.PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            if existing.amount_minor != amount or existing.currency != self.currency:
                raise TelegramPaymentError("Pending payment snapshot does not match current price")
            return existing

        try:
            return PaymentService.create_pending(
                order=order,
                provider=self.provider,
                amount_minor=amount,
                currency=self.currency,
                metadata={"channel": ChannelIdentity.Channel.TELEGRAM},
            )
        except PaymentError as exc:
            raise TelegramPaymentError(str(exc)) from exc

    @staticmethod
    def payload(payment: Payment) -> str:
        return f"order:{payment.order_id}:payment:{payment.pk}"

    def validate_pre_checkout(self, *, identity: ChannelIdentity, query: dict) -> Payment:
        payment = self._payment_from_payload(query.get("invoice_payload") or "")
        if payment.order.channel_identity_id != identity.pk:
            raise TelegramPaymentError("Payment does not belong to this Telegram user")
        if payment.status != Payment.Status.PENDING:
            raise TelegramPaymentError("Payment is no longer pending")
        if query.get("currency") != payment.currency:
            raise TelegramPaymentError("Payment currency does not match")
        if query.get("total_amount") != payment.amount_minor:
            raise TelegramPaymentError("Payment amount does not match")
        if payment.order.status != Order.Status.AWAITING_PAYMENT:
            raise TelegramPaymentError("Order is no longer awaiting payment")
        return payment

    def confirm_successful_payment(self, *, identity: ChannelIdentity, successful_payment: dict) -> Payment:
        payment = self._payment_from_payload(successful_payment.get("invoice_payload") or "")
        if payment.order.channel_identity_id != identity.pk:
            raise TelegramPaymentError("Payment does not belong to this Telegram user")
        if successful_payment.get("currency") != payment.currency:
            raise TelegramPaymentError("Payment currency does not match")
        if successful_payment.get("total_amount") != payment.amount_minor:
            raise TelegramPaymentError("Payment amount does not match")

        charge_id = successful_payment.get("telegram_payment_charge_id") or ""
        try:
            return PaymentService.confirm(
                payment=payment,
                external_payment_id=charge_id,
                metadata={
                    "telegram_payment_charge_id": charge_id,
                    "provider_payment_charge_id": successful_payment.get("provider_payment_charge_id") or "",
                },
            )
        except PaymentError as exc:
            raise TelegramPaymentError(str(exc)) from exc

    @staticmethod
    def _payment_from_payload(payload: str) -> Payment:
        parts = payload.split(":")
        if len(parts) != 4 or parts[0] != "order" or parts[2] != "payment":
            raise TelegramPaymentError("Invalid invoice payload")
        try:
            order_id = int(parts[1])
            payment_id = int(parts[3])
        except ValueError as exc:
            raise TelegramPaymentError("Invalid invoice payload") from exc

        payment = (
            Payment.objects.select_related("order", "order__channel_identity")
            .filter(pk=payment_id, order_id=order_id, provider=TelegramStarsPaymentAdapter.provider)
            .first()
        )
        if payment is None:
            raise TelegramPaymentError("Payment not found")
        return payment
