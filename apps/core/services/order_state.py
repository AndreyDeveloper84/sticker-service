from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.core.models import Order


class InvalidOrderTransition(ValueError):
    pass


@dataclass(frozen=True)
class OrderStateService:
    transitions = {
        Order.Status.DRAFT: {
            Order.Status.AWAITING_PHOTOS,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        },
        Order.Status.AWAITING_PHOTOS: {
            Order.Status.READY_FOR_CHECKOUT,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        },
        Order.Status.READY_FOR_CHECKOUT: {
            Order.Status.AWAITING_PAYMENT,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        },
        Order.Status.AWAITING_PAYMENT: {
            Order.Status.PAID,
            Order.Status.CANCELLED,
            Order.Status.FAILED,
        },
        Order.Status.PAID: {
            Order.Status.PREVIEW_GENERATING,
            Order.Status.FAILED,
        },
        Order.Status.PREVIEW_GENERATING: {
            Order.Status.INTERNAL_PREVIEW_REVIEW,
            Order.Status.FAILED,
        },
        Order.Status.INTERNAL_PREVIEW_REVIEW: {
            Order.Status.PREVIEW_GENERATING,
            Order.Status.PREVIEW_REVIEW,
            Order.Status.FAILED,
        },
        Order.Status.PREVIEW_REVIEW: {
            Order.Status.REVISION_REQUESTED,
            Order.Status.PACK_GENERATING,
            Order.Status.FAILED,
        },
        Order.Status.REVISION_REQUESTED: {
            Order.Status.REVISION_GENERATING,
            Order.Status.FAILED,
        },
        Order.Status.REVISION_GENERATING: {
            Order.Status.INTERNAL_PREVIEW_REVIEW,
            Order.Status.FAILED,
        },
        Order.Status.PACK_GENERATING: {
            Order.Status.QUALITY_CONTROL,
            Order.Status.FAILED,
        },
        Order.Status.QUALITY_CONTROL: {
            Order.Status.READY_FOR_DELIVERY,
            # QC FAIL selective retry: back to production for the failed
            # slots only (regeneration itself is DRF-2051 scope).
            Order.Status.PACK_GENERATING,
            Order.Status.FAILED,
        },
        Order.Status.READY_FOR_DELIVERY: {
            # DELIVERY_IN_PROGRESS / DELIVERED are added by DRF-2053.
            Order.Status.FAILED,
        },
        Order.Status.CANCELLED: set(),
        Order.Status.FAILED: set(),
    }

    @classmethod
    def allowed_targets(cls, status: str) -> set[str]:
        return set(cls.transitions.get(status, set()))

    @classmethod
    @transaction.atomic
    def transition(cls, *, order: Order, to_status: str) -> Order:
        if to_status == Order.Status.PAID:
            raise InvalidOrderTransition("PAID can only be reached through PaymentService")
        return cls._transition(order=order, to_status=to_status)

    @classmethod
    def _transition(cls, *, order: Order, to_status: str) -> Order:
        valid_statuses = {value for value, _label in Order.Status.choices}
        if to_status not in valid_statuses:
            raise InvalidOrderTransition(f"Unknown target status: {to_status}")

        allowed = cls.allowed_targets(order.status)
        if to_status not in allowed:
            raise InvalidOrderTransition(
                f"Transition {order.status} -> {to_status} is not allowed"
            )

        order.status = to_status
        order.save(update_fields=["status", "updated_at"])
        return order
