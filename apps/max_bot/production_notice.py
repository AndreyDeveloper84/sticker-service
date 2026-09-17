"""Customer-facing "in production" notice over MAX (best-effort, at most once).

After the customer approves the preview, full production and QC can take
hours; without this message the MAX dialog is silent until the final set
arrives. Mirrors ``paid_notice``:

- called from the Production Console AFTER ``FullProductionService.start``
  has committed the PREVIEW_REVIEW → PACK_GENERATING transition, never from
  inside the production transaction — a MAX failure cannot affect production;
- claim-then-send recorded in ``Payment.metadata["production_notice"]`` on
  the order's latest CONFIRMED payment (no new migration); repeated start
  calls (one slot per run) find the claim and do not send again;
- a failed send is recorded and not retried automatically (at most once);
- any exception is swallowed and logged.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Payment

logger = logging.getLogger(__name__)

PRODUCTION_NOTICE_TEXT = "Превью одобрено, стикеры в производстве. Пришлём готовый набор сюда."
PRODUCTION_NOTICE_KEY = "production_notice"

# Statuses that prove the order has entered full production (start() may
# already have moved a single-slot order past PACK_GENERATING).
IN_PRODUCTION_STATUSES = frozenset(
    {
        Order.Status.PACK_GENERATING,
        Order.Status.QUALITY_CONTROL,
        Order.Status.READY_FOR_DELIVERY,
        Order.Status.DELIVERY_IN_PROGRESS,
        Order.Status.DELIVERED,
    }
)


def _message_id(response) -> str:
    response = response or {}
    return str(
        response.get("body", {}).get("mid")
        or response.get("message", {}).get("mid")
        or response.get("mid")
        or ""
    )


@transaction.atomic
def _claim(order_id: int) -> tuple[Payment | None, str]:
    """Atomically mark the notice as in flight on the latest confirmed payment.

    Returns ``(payment, recipient_user_id)`` or ``(None, "")`` when nothing
    must be sent: order not on MAX, not in production, no confirmed payment,
    or a notice already claimed by an earlier start() run.
    """
    order = Order.objects.select_related("channel_identity").get(pk=order_id)
    identity = order.channel_identity
    if identity.channel != ChannelIdentity.Channel.MAX:
        return None, ""
    if order.status not in IN_PRODUCTION_STATUSES:
        return None, ""
    payment = (
        Payment.objects.select_for_update()
        .filter(order=order, status=Payment.Status.CONFIRMED)
        .order_by("-confirmed_at", "-pk")
        .first()
    )
    if payment is None:
        return None, ""
    metadata = dict(payment.metadata or {})
    if metadata.get(PRODUCTION_NOTICE_KEY):
        return None, ""
    metadata[PRODUCTION_NOTICE_KEY] = {
        "status": "pending",
        "claimed_at": timezone.now().isoformat(),
    }
    payment.metadata = metadata
    payment.save(update_fields=["metadata", "updated_at"])
    return payment, identity.external_user_id


@transaction.atomic
def _record(payment_id: int, *, status: str, message_id: str = "", error: str = "") -> None:
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    metadata = dict(payment.metadata or {})
    notice = dict(metadata.get(PRODUCTION_NOTICE_KEY) or {})
    notice.update(
        {
            "status": status,
            "message_id": message_id,
            "error": error,
            "finished_at": timezone.now().isoformat(),
        }
    )
    metadata[PRODUCTION_NOTICE_KEY] = notice
    payment.metadata = metadata
    payment.save(update_fields=["metadata", "updated_at"])


def notify_customer_production_started(*, order: Order, client) -> bool:
    """Tell the MAX customer that production has started.

    Returns True only when a message was sent by this call. Never raises.
    """
    try:
        payment, recipient = _claim(order.pk)
        if payment is None:
            return False
        try:
            response = client.send_message(user_id=recipient, text=PRODUCTION_NOTICE_TEXT)
        except Exception as exc:  # noqa: BLE001 - best-effort customer boundary
            logger.warning(
                "max.production_notice.send_failed order=%s error=%s",
                order.pk,
                exc.__class__.__name__,
            )
            _record(payment.pk, status="failed", error=exc.__class__.__name__)
            return False
        _record(payment.pk, status="sent", message_id=_message_id(response))
        return True
    except Exception:  # noqa: BLE001 - the notice must never affect production
        logger.exception("max.production_notice.unexpected order=%s", order.pk)
        return False
