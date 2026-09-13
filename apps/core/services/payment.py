from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import Order, Payment
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService


class PaymentError(ValueError):
    pass


@dataclass(frozen=True)
class PaymentService:
    @classmethod
    @transaction.atomic
    def create_pending(
        cls,
        *,
        order: Order,
        provider: str,
        amount_minor: int,
        currency: str,
        metadata: dict | None = None,
    ) -> Payment:
        if amount_minor <= 0:
            raise PaymentError("Payment amount must be positive")
        currency = currency.strip().upper()
        if not currency:
            raise PaymentError("Payment currency is required")
        provider = provider.strip()
        if not provider:
            raise PaymentError("Payment provider is required")

        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        if locked_order.status == Order.Status.READY_FOR_CHECKOUT:
            OrderStateService.transition(
                order=locked_order,
                to_status=Order.Status.AWAITING_PAYMENT,
            )
        elif locked_order.status != Order.Status.AWAITING_PAYMENT:
            raise PaymentError(
                f"Order #{locked_order.pk} is not ready for payment: {locked_order.status}"
            )

        return Payment.objects.create(
            order=locked_order,
            provider=provider,
            amount_minor=amount_minor,
            currency=currency,
            metadata=metadata or {},
        )

    @classmethod
    @transaction.atomic
    def confirm(
        cls,
        *,
        payment: Payment,
        external_payment_id: str,
        metadata: dict | None = None,
    ) -> Payment:
        external_payment_id = external_payment_id.strip()
        if not external_payment_id:
            raise PaymentError("External payment id is required")

        locked_payment = (
            Payment.objects.select_for_update()
            .select_related("order")
            .get(pk=payment.pk)
        )
        locked_order = Order.objects.select_for_update().get(pk=locked_payment.order_id)

        if locked_payment.status == Payment.Status.CONFIRMED:
            if locked_payment.external_payment_id != external_payment_id:
                raise PaymentError("Confirmed payment id cannot be changed")
            return locked_payment

        if locked_payment.status != Payment.Status.PENDING:
            raise PaymentError(
                f"Payment #{locked_payment.pk} cannot be confirmed from {locked_payment.status}"
            )

        duplicate = Payment.objects.filter(
            provider=locked_payment.provider,
            external_payment_id=external_payment_id,
        ).exclude(pk=locked_payment.pk)
        if duplicate.exists():
            raise PaymentError("Provider transaction is already bound to another payment")

        if locked_order.status != Order.Status.AWAITING_PAYMENT:
            raise PaymentError(
                f"Order #{locked_order.pk} is not awaiting payment: {locked_order.status}"
            )

        merged_metadata = dict(locked_payment.metadata or {})
        if metadata:
            merged_metadata.update(metadata)

        locked_payment.external_payment_id = external_payment_id
        locked_payment.status = Payment.Status.CONFIRMED
        locked_payment.confirmed_at = timezone.now()
        locked_payment.metadata = merged_metadata
        try:
            locked_payment.save(
                update_fields=[
                    "external_payment_id",
                    "status",
                    "confirmed_at",
                    "metadata",
                    "updated_at",
                ]
            )
        except IntegrityError as exc:
            raise PaymentError("Provider transaction is already confirmed") from exc

        try:
            OrderStateService._transition(
                order=locked_order,
                to_status=Order.Status.PAID,
            )
        except InvalidOrderTransition as exc:
            raise PaymentError(str(exc)) from exc

        return locked_payment
