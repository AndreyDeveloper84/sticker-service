"""Customer-facing PAID confirmation over MAX (best-effort, at most once).

The YooKassa webhook confirms the payment first (its own committed
transaction, see ``PaymentService.confirm``); only afterwards is the customer
told. The notice never influences the payment outcome:

- claim-then-send: a ``paid_notice`` record is written into
  ``Payment.metadata`` before the MAX call, so a duplicate webhook (YooKassa
  retries for up to 24h) finds the claim and does not send again;
- a failed send is recorded as ``failed`` and NOT retried automatically
  (at-most-once — never a duplicate customer message); operators see the
  outcome in the payment metadata;
- any exception is swallowed and logged: the webhook still ACKs YooKassa.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.models import ChannelIdentity, Payment
from apps.max_bot.client import created_message_id

logger = logging.getLogger(__name__)

PAID_NOTICE_TEXT = "Оплата получена. Готовим ваше превью."
PAID_NOTICE_KEY = "paid_notice"


def _message_id(response) -> str:
    return created_message_id(response)


@transaction.atomic
def _claim(payment_id: int) -> tuple[Payment | None, str]:
    """Atomically mark the payment as "notice in flight".

    Returns ``(payment, recipient_user_id)`` or ``(None, "")`` when nothing
    must be sent: payment not CONFIRMED, order not on MAX, or a notice was
    already claimed by an earlier webhook delivery.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("order__channel_identity")
        .get(pk=payment_id)
    )
    if payment.status != Payment.Status.CONFIRMED:
        return None, ""
    identity = payment.order.channel_identity
    if identity.channel != ChannelIdentity.Channel.MAX:
        return None, ""
    metadata = dict(payment.metadata or {})
    if metadata.get(PAID_NOTICE_KEY):
        return None, ""
    metadata[PAID_NOTICE_KEY] = {
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
    notice = dict(metadata.get(PAID_NOTICE_KEY) or {})
    notice.update(
        {
            "status": status,
            "message_id": message_id,
            "error": error,
            "finished_at": timezone.now().isoformat(),
        }
    )
    metadata[PAID_NOTICE_KEY] = notice
    payment.metadata = metadata
    payment.save(update_fields=["metadata", "updated_at"])


def notify_customer_paid(*, payment: Payment, client) -> bool:
    """Send the PAID confirmation for a confirmed MAX payment.

    Returns True only when a message was sent by this call. Never raises.
    """
    try:
        claimed, recipient = _claim(payment.pk)
        if claimed is None:
            return False
        try:
            response = client.send_message(user_id=recipient, text=PAID_NOTICE_TEXT)
        except Exception as exc:  # noqa: BLE001 - best-effort customer boundary
            logger.warning(
                "max.paid_notice.send_failed payment=%s error=%s",
                payment.pk,
                exc.__class__.__name__,
            )
            _record(payment.pk, status="failed", error=exc.__class__.__name__)
            return False
        _record(payment.pk, status="sent", message_id=_message_id(response))
        return True
    except Exception:  # noqa: BLE001 - the notice must never affect the webhook
        logger.exception("max.paid_notice.unexpected payment=%s", payment.pk)
        return False
