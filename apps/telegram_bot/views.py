import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

import logging

from apps.core.customer_hints import (
    PHOTO_GUIDANCE,
    REVISION_ACCEPTED_TEXT,
    REVISION_CLOTHES_ACCEPTED_TEXT,
    REVISION_TEXT_SAVED_TEXT,
    customer_hint,
)
from apps.core.bot_menu import PAYLOAD_CONFIRM_ORDER, OrderStepper
from apps.core.models import Order, Revision
from apps.core.services.channel_order_flow import PILOT_CONSENT_BUTTON_LABEL, PILOT_CONSENT_TEXT
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.telegram_bot.adapter import TelegramAdapter, TelegramFlowError
from apps.telegram_bot.client import TelegramAPIError, TelegramBotClient
from apps.telegram_bot.paid_notice import notify_customer_paid
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter, configured_stars_price

logger = logging.getLogger(__name__)


@csrf_exempt
def webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return HttpResponse(status=403)

    try:
        update = json.loads(request.body)
    except (TypeError, ValueError):
        return HttpResponse(status=400)

    adapter = TelegramAdapter()
    payment_adapter = TelegramStarsPaymentAdapter()
    client = TelegramBotClient(settings.TELEGRAM_BOT_TOKEN)

    try:
        _handle_update(update, adapter=adapter, payment_adapter=payment_adapter, client=client)
    except TelegramPaymentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)})
    except (TelegramFlowError, PreviewFeedbackError) as exc:
        # Out-of-step input (photo before /start, stale button, ...): tell
        # the customer what to do next and ACK the update — a bare 409 left
        # the bot silent (DRF-2083).
        _reply_hint(client, update, exc)
        return JsonResponse({"ok": True, "hint": str(exc)})

    return JsonResponse({"ok": True})


def _update_chat_id(update):
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def _reply_hint(client, update, exc: Exception) -> None:
    """Best-effort customer hint for a rejected step; never raises."""
    chat_id = _update_chat_id(update)
    if chat_id is not None:
        try:
            client.send_message(chat_id=chat_id, text=customer_hint(exc))
        except TelegramAPIError as api_exc:
            logger.warning("telegram.webhook.hint_failed error=%s", api_exc)
    callback = update.get("callback_query") or {}
    if callback.get("id"):
        try:
            client.answer_callback_query(callback_query_id=callback["id"])
        except TelegramAPIError as api_exc:
            logger.warning("telegram.webhook.callback_ack_failed error=%s", api_exc)


def _handle_update(update, *, adapter, payment_adapter, client):
    if "pre_checkout_query" in update:
        return _handle_pre_checkout(update["pre_checkout_query"], adapter=adapter, payment_adapter=payment_adapter, client=client)
    if "message" in update:
        return _handle_message(update["message"], adapter=adapter, payment_adapter=payment_adapter, client=client)
    if "callback_query" in update:
        return _handle_callback(update["callback_query"], adapter=adapter, payment_adapter=payment_adapter, client=client)
    return None


def _handle_pre_checkout(query, *, adapter, payment_adapter, client):
    identity = adapter.get_or_create_identity(query["from"])
    try:
        payment_adapter.validate_pre_checkout(identity=identity, query=query)
    except TelegramPaymentError as exc:
        return client.answer_pre_checkout_query(pre_checkout_query_id=query["id"], ok=False, error_message=str(exc))
    return client.answer_pre_checkout_query(pre_checkout_query_id=query["id"], ok=True)


def _reply_markup(rows):
    """bot_menu rows [(label, payload)] → Telegram inline keyboard."""
    return {"inline_keyboard": [[{"text": label, "callback_data": payload} for label, payload in row] for row in rows]}


def _stepper(*, chat_id, adapter, client):
    def send(text, rows):
        if rows:
            return client.send_message(chat_id=chat_id, text=text, reply_markup=_reply_markup(rows))
        return client.send_message(chat_id=chat_id, text=text)

    return OrderStepper(adapter=adapter, send=send, telegram=True)


def _handle_message(message, *, adapter, payment_adapter, client):
    identity = adapter.get_or_create_identity(message["from"])
    chat_id = message["chat"]["id"]
    stepper = _stepper(chat_id=chat_id, adapter=adapter, client=client)

    successful_payment = message.get("successful_payment")
    if successful_payment:
        payment = payment_adapter.confirm_successful_payment(identity=identity, successful_payment=successful_payment)
        # PAID is committed above. The confirmation is best-effort and at most
        # once per payment (a redelivered update finds the claim and sends
        # nothing); a Telegram send failure must not turn a confirmed payment
        # into a non-2xx answer, which would only make Telegram redeliver.
        notify_customer_paid(payment=payment, client=client, chat_id=chat_id)
        return None

    if (message.get("text") or "").startswith("/start"):
        return stepper.main_menu(identity)

    photos = message.get("photo") or []
    if photos:
        # Flow check before the download: a photo that no order is waiting
        # for is answered with a hint, never fetched from Telegram.
        adapter.current_photo_order(identity)
        file_info = client.get_file(photos[-1]["file_id"])
        file_path = file_info["file_path"]
        content = client.download_file(file_path)
        mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        adapter.save_photo_bytes(identity=identity, content=content, filename=file_path.rsplit("/", 1)[-1], mime_type=mime_type)
        return stepper.photo_saved(identity)

    # optional «во что переодеть» after the «Сменить одежду» revision button
    revision = PreviewFeedbackService.attach_revision_text(identity=identity, text=message.get("text") or "")
    if revision is not None:
        client.send_message(chat_id=chat_id, text=REVISION_TEXT_SAVED_TEXT.format(text=revision.customer_text))
        return None

    # free text: the 9 phrases or the name/contact, when the bot asked for them
    stepper.handle_text(identity, message.get("text") or "")
    return None


def _feedback_order(identity):
    order = identity.orders.filter(status=Order.Status.PREVIEW_REVIEW).order_by("-created_at").first()
    if not order:
        raise PreviewFeedbackError("Нет превью, ожидающего вашей оценки")
    return order


PHOTO_PROMPT = PHOTO_GUIDANCE

# Consent gate (parity with MAX, DRF-2069): shown after the photos are
# complete, before the order summary and the Stars invoice; the accepted text
# version is persisted on the order so checkout can fail closed without it.
CONSENT_TEXT = PILOT_CONSENT_TEXT
CONSENT_REPLY_MARKUP = {"inline_keyboard": [[{"text": PILOT_CONSENT_BUTTON_LABEL, "callback_data": "consent:accept"}]]}
PAY_REPLY_MARKUP = {"inline_keyboard": [[{"text": "Оплатить", "callback_data": "pay"}]]}


def _summary_text(summary, *, price_stars):
    lines = [f"Ваш заказ: {summary['product_name']}", f"Стиль: {summary['style_name']}"]
    if summary["quantity"]:
        lines.append(f"Стикеров: {summary['quantity']}")
    if summary["emotions"]:
        label = "Надписи" if summary.get("captioned") else "Эмоции"
        lines.append(f"{label}: {', '.join(summary['emotions'])}")
    if summary.get("contact"):
        lines.append(f"Контакт: {summary['contact']}")
    if price_stars:
        lines.append(f"Цена: {price_stars} Stars")
    return "\n".join(lines)


def _summary_stars_price(product):
    # Shows exactly what the XTR invoice will charge; never a RUB amount.
    try:
        return configured_stars_price(product)
    except TelegramPaymentError:
        return None


def _revision_accepted_text(revision):
    if revision.category == Revision.Category.CLOTHES and not revision.customer_text:
        return REVISION_CLOTHES_ACCEPTED_TEXT
    return REVISION_ACCEPTED_TEXT


def _revision_buttons():
    labels = {
        Revision.Category.FACE: "Лицо",
        Revision.Category.HAIR: "Волосы",
        Revision.Category.BODY: "Тело",
        Revision.Category.DETAIL: "Детали",
        Revision.Category.COLORS: "Цвета",
        Revision.Category.STYLE_EXPECTATION: "Стиль",
        Revision.Category.CLOTHES: "Сменить одежду",
        Revision.Category.OTHER: "Другое",
    }
    return [[{"text": label, "callback_data": f"preview_revision:{value}"}] for value, label in labels.items()]


def _handle_callback(callback, *, adapter, payment_adapter, client):
    identity = adapter.get_or_create_identity(callback["from"])
    chat_id = callback["message"]["chat"]["id"]
    data = callback.get("data") or ""
    stepper = _stepper(chat_id=chat_id, adapter=adapter, client=client)

    if stepper.handle_callback(identity, data):
        pass  # menu / navigation / product / style / emotions / photos_done
    elif data == PAYLOAD_CONFIRM_ORDER:
        # every step complete → the existing consent screen (pilot contract)
        stepper.confirm_order(identity)
        client.send_message(chat_id=chat_id, text=CONSENT_TEXT, reply_markup=CONSENT_REPLY_MARKUP)
    elif data == "consent:accept":
        order = adapter.accept_consent(identity=identity)
        if order.status == Order.Status.AWAITING_PHOTOS:
            order = adapter.complete_photos(identity)
        # Repeated accept is idempotent: consent is stored once; "pay" reuses
        # the pending payment.
        client.send_message(chat_id=chat_id, text=_summary_text(adapter.order_summary(order), price_stars=_summary_stars_price(order.product)), reply_markup=PAY_REPLY_MARKUP)
    elif data == "pay":
        payment = payment_adapter.payment_for_identity(identity)
        client.send_invoice(chat_id=chat_id, title=payment.order.product.name, description="Персональный цифровой заказ", payload=payment_adapter.payload(payment), amount_stars=payment.amount_minor)
    elif data == "preview_approve":
        PreviewFeedbackService.approve(order=_feedback_order(identity))
        client.send_message(chat_id=chat_id, text="Превью принято. Спасибо!")
    elif data == "preview_revision":
        client.send_message(
            chat_id=chat_id,
            text="Что нужно исправить? Выберите основную причину.",
            reply_markup={"inline_keyboard": _revision_buttons()},
        )
    elif data.startswith("preview_revision:"):
        category = data.split(":", 1)[1]
        revision = PreviewFeedbackService.request_revision(
            order=_feedback_order(identity),
            category=category,
        )
        client.send_message(chat_id=chat_id, text=_revision_accepted_text(revision))

    return client.answer_callback_query(callback_query_id=callback["id"])
