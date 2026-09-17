"""Customer-facing PAID confirmation over Telegram (best-effort, at most once).

Parity with ``apps.max_bot.paid_notice`` (DRF-2069). The Stars payment is
confirmed first (``PaymentService.confirm``, its own committed transaction);
only afterwards is the customer told, and the notice never influences the
webhook outcome:

- claim-then-send: a ``paid_notice`` record is written into
  ``Payment.metadata`` before the Telegram call, so a redelivered
  ``successful_payment`` update (Telegram retries on non-2xx / timeout)
  finds the claim and does not send again;
- a failed send is recorded as ``failed`` and NOT retried automatically
  (at-most-once — never a duplicate customer message);
- any exception is swallowed and logged: the webhook still answers 200.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.models import ChannelIdentity, Payment

logger = logging.getLogger(__name__)

PAID_NOTICE_TEXT = "Оплата получена. Начинаем подготовку превью."
PAID_NOTICE_KEY = "paid_notice"


def _message_id(response) -> str:
    response = response or {}
    if not isinstance(response, dict):
        return ""
    result = response.get("result") if isinstance(response.get("result"), dict) else response
    return str(result.get("message_id") or "")


@transaction.atomic
def _claim(payment_id: int) -> Payment | None:
    """Atomically mark the payment as "notice in flight".

    Returns the payment, or None when nothing must be sent: payment not
    CONFIRMED, order not on Telegram, or a notice already claimed by an
    earlier delivery of the same update.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("order__channel_identity")
        .get(pk=payment_id)
    )
    if payment.status != Payment.Status.CONFIRMED:
        return None
    if payment.order.channel_identity.channel != ChannelIdentity.Channel.TELEGRAM:
        return None
    metadata = dict(payment.metadata or {})
    if metadata.get(PAID_NOTICE_KEY):
        return None
    metadata[PAID_NOTICE_KEY] = {
        "status": "pending",
        "claimed_at": timezone.now().isoformat(),
    }
    payment.metadata = metadata
    payment.save(update_fields=["metadata", "updated_at"])
    return payment


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


def notify_customer_paid(*, payment: Payment, client, chat_id) -> bool:
    """Send the PAID confirmation for a confirmed Telegram payment into the
    dialog the ``successful_payment`` update arrived from.

    Returns True only when a message was sent by this call. Never raises.
    """
    try:
        claimed = _claim(payment.pk)
        if claimed is None:
            return False
        try:
            response = client.send_message(chat_id=chat_id, text=PAID_NOTICE_TEXT)
        except Exception as exc:  # noqa: BLE001 - best-effort customer boundary
            logger.warning(
                "telegram.paid_notice.send_failed payment=%s error=%s",
                payment.pk,
                exc.__class__.__name__,
            )
            _record(payment.pk, status="failed", error=exc.__class__.__name__)
            return False
        _record(payment.pk, status="sent", message_id=_message_id(response))
        return True
    except Exception:  # noqa: BLE001 - the notice must never affect the webhook
        logger.exception("telegram.paid_notice.unexpected payment=%s", payment.pk)
        return False
