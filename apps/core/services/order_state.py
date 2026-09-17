from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.core.models import Order, OrderEvent


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
            # QC exits (DRF-2052): PASS -> READY_FOR_DELIVERY; FAIL with
            # selective retry -> PACK_GENERATING so DRF-2051 regenerates
            # exactly the requested slot_keys.
            Order.Status.READY_FOR_DELIVERY,
            Order.Status.PACK_GENERATING,
            Order.Status.FAILED,
        },
        Order.Status.READY_FOR_DELIVERY: {
            # QC PASS exit (DRF-2052); delivery entry is DRF-2053.
            Order.Status.DELIVERY_IN_PROGRESS,
            Order.Status.FAILED,
        },
        # --- Final delivery (DRF-2053 ownership) ---
        Order.Status.DELIVERY_IN_PROGRESS: {
            # Recovery after a partial/failed run is an operator "resume"
            # from DELIVERY_IN_PROGRESS itself (already-sent slots stay
            # sent); there is no way back to READY_FOR_DELIVERY.
            Order.Status.DELIVERED,
            Order.Status.FAILED,
        },
        Order.Status.DELIVERED: set(),
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

        from_status = order.status
        order.status = to_status
        order.save(update_fields=["status", "updated_at"])
        # DRF-2055: single emission point for the pilot funnel; every
        # transition (including PAID via PaymentService) passes through here.
        OrderEvent.objects.create(
            order=order,
            event_type=OrderEvent.Type.STATUS_CHANGED,
            from_status=from_status,
            to_status=to_status,
        )
        return order
