import json
import logging
import os
from mimetypes import guess_type
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

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
from apps.core.services.media import ALLOWED_IMAGE_MIME_TYPES
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.max_bot.adapter import MaxAdapter, MaxFlowError
from apps.max_bot.checkout import start_checkout
from apps.max_bot.client import MaxAPIError, MaxBotClient
from apps.max_bot.parser import MaxEvent, MaxParseError, parse_max_event
from apps.max_bot.photo import (
    PhotoDownloadError,
    PhotoTooLargeError,
    download_photo,
    extract_photo_url,
)


@csrf_exempt
def webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = os.getenv("MAX_WEBHOOK_SECRET", "")
    if secret and request.headers.get("X-Max-Bot-Api-Secret") != secret:
        return HttpResponse(status=403)

    try:
        update = json.loads(request.body)
    except (TypeError, ValueError):
        return HttpResponse(status=400)

    try:
        event = parse_max_event(update)
    except MaxParseError:
        return HttpResponse(status=400)

    adapter = MaxAdapter()
    client = MaxBotClient(os.getenv("MAX_BOT_TOKEN", ""))
    try:
        _handle_event(event, adapter=adapter, client=client)
    except (MaxFlowError, PreviewFeedbackError) as exc:
        # Out-of-step input (photo before /start, stale button, ...): tell
        # the customer what to do next and ACK the update — a bare 409 left
        # the bot silent and only made MAX redeliver (DRF-2083).
        _reply_hint(client, event, exc)
        return JsonResponse({"ok": True, "hint": str(exc)})
    except MaxAPIError as exc:
        # MAX error bodies carry a code/message pair, never secrets.
        logger.warning(
            "max.webhook.api_error update_type=%s status=%s body=%r",
            event.update_type,
            exc.status_code,
            exc.body[:200],
        )
        return JsonResponse({"ok": False, "error": "max api error", "status": exc.status_code}, status=502)

    return JsonResponse({"ok": True})


def _reply(client, event: MaxEvent, **kwargs):
    """Reply to an inbound event in the dialog it arrived from.

    Reference addressing rule (ai-bot-platform DRF-1558): an inbound event's
    ``chat_id`` is correct by construction; a stored ``chat_id`` must never
    be used as a universal user address. Bot-initiated sends (checkout is
    triggered by a callback and gets the event's chat_id; preview delivery
    from the console is proactive and stays on ``user_id``).
    """
    return client.send_message(chat_id=event.chat_id, **kwargs)


def _reply_hint(client, event: MaxEvent, exc: Exception) -> None:
    """Best-effort customer hint for a rejected step; never raises."""
    try:
        _reply(client, event, text=customer_hint(exc))
    except MaxAPIError as api_exc:
        logger.warning(
            "max.webhook.hint_failed update_type=%s status=%s body=%r",
            event.update_type,
            api_exc.status_code,
            api_exc.body[:200],
        )
    if event.callback_id:
        try:
            client.answer_callback(callback_id=event.callback_id)
        except MaxAPIError as api_exc:
            logger.warning("max.webhook.callback_ack_failed status=%s body=%r", api_exc.status_code, api_exc.body[:200])


def _buttons(rows):
    """bot_menu rows [(label, payload)] → MAX callback buttons."""
    return [[{"text": label, "payload": payload} for label, payload in row] for row in rows]


def _stepper(event: MaxEvent, *, adapter, client):
    def send(text, rows):
        if rows:
            return _reply(client, event, text=text, buttons=_buttons(rows))
        return _reply(client, event, text=text)

    return OrderStepper(adapter=adapter, send=send, telegram=False)


def _handle_event(event: MaxEvent, *, adapter, client):
    if event.update_type == "bot_started":
        identity = adapter.get_or_create_identity(event.user)
        return _stepper(event, adapter=adapter, client=client).main_menu(identity)
    if event.update_type == "message_created":
        return _handle_message(event, adapter=adapter, client=client)
    if event.update_type == "message_callback":
        return _handle_callback(event, adapter=adapter, client=client)
    return None


def _handle_message(event: MaxEvent, *, adapter, client):
    identity = adapter.get_or_create_identity(event.user)
    stepper = _stepper(event, adapter=adapter, client=client)

    if event.text.startswith("/start"):
        return stepper.main_menu(identity)
    if event.text.split()[:1] == ["/status"]:
        return stepper.status(identity)

    for attachment in event.attachments:
        image_url = _photo_attachment_url(attachment)
        if not image_url:
            continue
        # Flow check before the download: a photo that no order is waiting
        # for is answered with a hint, never fetched from the CDN.
        adapter.current_photo_order(identity)
        try:
            content = download_photo(image_url)
        except PhotoTooLargeError:
            return _reply(client, event, text="Фото слишком большое (больше 10 МБ). Отправьте файл поменьше.")
        except PhotoDownloadError:
            return _reply(client, event, text="Не удалось загрузить фото. Отправьте его ещё раз.")
        filename = urlparse(image_url).path.rsplit("/", 1)[-1] or "photo.jpg"
        mime_type = guess_type(filename)[0] or "image/jpeg"
        try:
            adapter.save_photo_bytes(
                identity=identity,
                content=content,
                filename=filename,
                mime_type=mime_type,
            )
        except ValidationError:
            # MediaService rejects unsupported MIME types / sizes; answer the
            # customer instead of letting a 500 trigger MAX redelivery.
            return _reply(client, event, text=PHOTO_REJECTED)
        return stepper.photo_saved(identity)

    if not event.text or event.text.startswith("/") or event.attachments:
        # a sticker, a non-image file, an unknown command: never silence —
        # the current step again (or the main menu)
        stepper.reprompt(identity)
        return None

    # optional «во что переодеть» after the «Сменить одежду» revision button
    revision = PreviewFeedbackService.attach_revision_text(identity=identity, text=event.text)
    if revision is not None:
        return _reply(client, event, text=REVISION_TEXT_SAVED_TEXT.format(text=revision.customer_text))

    # free text: the 9 phrases or the name/contact when the bot asked for
    # them; anything else re-prompts the current step
    stepper.handle_text(identity, event.text)
    return None


def _photo_attachment_url(attachment) -> str | None:
    """URL of a customer photo: an ``image`` attachment, or a ``file``
    attachment that is an image by its name (a photo sent «as a file»)."""
    if not isinstance(attachment, dict):
        return None
    if attachment.get("type") == "image":
        return extract_photo_url(attachment)
    if attachment.get("type") == "file":
        payload = attachment.get("payload") or {}
        url = payload.get("url")
        name = str(payload.get("filename") or payload.get("name") or "")
        mime_type = guess_type(name)[0] or ""
        if isinstance(url, str) and url and mime_type in ALLOWED_IMAGE_MIME_TYPES:
            return url
    return None


def _feedback_order(identity):
    order = identity.orders.filter(status=Order.Status.PREVIEW_REVIEW).order_by("-created_at").first()
    if not order:
        raise PreviewFeedbackError("Нет превью, ожидающего вашей оценки")
    return order


PHOTO_PROMPT = PHOTO_GUIDANCE

PHOTO_REJECTED = (
    "Не удалось принять фото. Поддерживаются JPEG, PNG и WebP до 10 МБ. "
    "Отправьте другое фото."
)

# Consent gate (MAX Pilot): shown after the photos are complete, before the
# order summary and the YooKassa link. The accepted text version is persisted
# on the order (PILOT_CONSENT_VERSION) so checkout can fail closed without it.
CONSENT_TEXT = PILOT_CONSENT_TEXT
CONSENT_BUTTONS = [[{"text": PILOT_CONSENT_BUTTON_LABEL, "payload": "consent:accept"}]]


def _summary_text(summary):
    lines = [f"Ваш заказ: {summary['product_name']}", f"Стиль: {summary['style_name']}"]
    if summary["quantity"]:
        lines.append(f"Стикеров: {summary['quantity']}")
    if summary["emotions"]:
        label = "Надписи" if summary.get("captioned") else "Эмоции"
        lines.append(f"{label}: {', '.join(summary['emotions'])}")
    if summary.get("contact"):
        lines.append(f"Контакт: {summary['contact']}")
    if summary["price_minor"]:
        lines.append(f"Цена: {summary['price_minor'] // 100} ₽")
    return "\n".join(lines)


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
    return [[{"text": label, "payload": f"preview_revision:{value}"}] for value, label in labels.items()]


def _drop_keyboard(client, event: MaxEvent) -> None:
    """Best-effort: the keyboard of the message whose button was just
    pressed is now behind the customer — remove it (PUT /messages with an
    empty attachments list) so old buttons stop living on. A failure never
    breaks the step."""
    if not event.message_id:
        return
    try:
        client.edit_message(message_id=event.message_id, attachments=[])
    except MaxAPIError as exc:
        logger.info("max.webhook.drop_keyboard_failed status=%s body=%r", exc.status_code, exc.body[:200])


def _handle_callback(event: MaxEvent, *, adapter, client):
    identity = adapter.get_or_create_identity(event.user)
    payload = event.callback_payload
    stepper = _stepper(event, adapter=adapter, client=client)

    if stepper.handle_callback(identity, payload):
        # menu / navigation / product / style / emotions / photos_done: the
        # customer moved on — the pressed message's keyboard is stale now
        _drop_keyboard(client, event)
    elif payload == PAYLOAD_CONFIRM_ORDER:
        # every step complete → the existing consent screen (pilot contract)
        stepper.confirm_order(identity)
        _reply(client, event, text=CONSENT_TEXT, buttons=CONSENT_BUTTONS)
    elif payload == "consent:accept":
        order = adapter.accept_consent(identity=identity)
        if order.status == Order.Status.AWAITING_PHOTOS:
            order = adapter.complete_photos(identity)
        # Repeated accept is idempotent: consent is stored once, the pending
        # checkout session (if any) is reused by start_checkout.
        _reply(client, event, text=_summary_text(adapter.order_summary(order)))
        start_checkout(identity=identity, client=client, chat_id=event.chat_id)
    elif payload == "preview_approve":
        PreviewFeedbackService.approve(order=_feedback_order(identity))
        _reply(client, event, text="Превью принято. Спасибо!")
    elif payload == "preview_revision":
        _reply(
            client,
            event,
            text="Что нужно исправить? Выберите основную причину.",
            buttons=_revision_buttons(),
        )
    elif payload.startswith("preview_revision:"):
        category = payload.split(":", 1)[1]
        revision = PreviewFeedbackService.request_revision(
            order=_feedback_order(identity),
            category=category,
        )
        _reply(client, event, text=_revision_accepted_text(revision))

    # ACK the callback after successful handling (POST /answers?callback_id=).
    # Best-effort: the reply above is already the user-visible answer; an ACK
    # failure must not 502 the webhook — MAX would retry the update and the
    # user would get the reply again (observed on staging 2026-09-14).
    if event.callback_id:
        try:
            return client.answer_callback(callback_id=event.callback_id)
        except MaxAPIError as exc:
            logger.warning(
                "max.webhook.callback_ack_failed status=%s body=%r",
                exc.status_code,
                exc.body[:200],
            )
    return None
