import json
import logging
import os
from mimetypes import guess_type
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

from apps.core.customer_hints import PHOTO_GUIDANCE, SINGLE_PHOTO_REMINDER, customer_hint
from apps.core.bot_menu import CONTACT_TEXT, EXAMPLES_TEXT, MAIN_MENU, ORDER_HOW_IT_WORKS, PHOTO_REQUIREMENTS, main_menu_rows, prices_text
from apps.core.models import Order, Revision
from apps.core.services.channel_order_flow import PILOT_CONSENT_BUTTON_LABEL, PILOT_CONSENT_TEXT, product_requires_custom_phrases, product_requires_customer_contact
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


def _handle_event(event: MaxEvent, *, adapter, client):
    if event.update_type == "bot_started":
        return _send_main_menu(event, client=client)
    if event.update_type == "message_created":
        return _handle_message(event, adapter=adapter, client=client)
    if event.update_type == "message_callback":
        return _handle_callback(event, adapter=adapter, client=client)
    return None


def _send_products(event: MaxEvent, *, adapter, client):
    buttons = [[{"text": product.name, "payload": f"product:{product.code}"}] for product in adapter.active_products()]
    buttons.append([{"text": "🏠 Главное меню", "payload": "menu:main"}])
    return _reply(client, event, text="Выберите вариант", buttons=buttons)


def _send_main_menu(event: MaxEvent, *, client):
    buttons = [[{"text": label, "payload": payload}] for label, payload in main_menu_rows("menu")]
    return _reply(client, event, text=MAIN_MENU, buttons=buttons)


def _handle_message(event: MaxEvent, *, adapter, client):
    identity = adapter.get_or_create_identity(event.user)

    if event.text.startswith("/start"):
        return _send_main_menu(event, client=client)

    for attachment in event.attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "image":
            continue
        image_url = extract_photo_url(attachment)
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
        return _reply(
            client,
            event,
            text="Фото сохранено. Отправьте ещё или нажмите «Фото загружены».",
            buttons=[[{"text": "Фото загружены", "payload": "photos_done"}]],
        )
    order = identity.orders.filter(status=Order.Status.AWAITING_PHOTOS).order_by("-id").first()
    awaiting = str((order.selection or {}).get("awaiting_input") or "") if order else ""
    if awaiting == "phrases" and event.text.strip():
        adapter.save_custom_phrases(identity=identity, text=event.text)
        adapter.set_awaiting_input(identity=identity, value="contact")
        return _reply(client, event, text="Оставьте имя и удобный способ связи: @username, телефон или ссылку.")
    if awaiting == "contact" and event.text.strip():
        order = adapter.save_customer_contact(identity=identity, text=event.text)
        return _reply(client, event, text=_summary_text(adapter.order_summary(order)), buttons=[[{"text": "✅ Подтвердить заказ", "payload": "order:confirm"}]])
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


def _emotion_step(adapter, order):
    """Emotion step text + buttons, driven entirely by Product.config.

    A product whose deterministic emotion set matches the required count
    (sticker pack) is confirmed as a whole; otherwise emotions are picked
    one by one (single sticker).
    """
    options = adapter.emotion_options(product=order.product)
    required = adapter.required_emotion_count(product=order.product)
    if required == len(options):
        labels = ", ".join(option["label"] for option in options)
        text = f"В набор входят {required} эмоций: {labels}."
        buttons = [[{"text": "Подтвердить набор", "payload": "emotions:confirm"}]]
    else:
        text = "Выберите эмоцию для стикера."
        buttons = [[{"text": option["label"], "payload": f"emotion:{option['code']}"}] for option in options]
    return text, buttons


def _summary_text(summary):
    lines = [f"Ваш заказ: {summary['product_name']}", f"Стиль: {summary['style_name']}"]
    if summary["quantity"]:
        lines.append(f"Стикеров: {summary['quantity']}")
    if summary["emotions"]:
        lines.append(f"Эмоции: {', '.join(summary['emotions'])}")
    if summary["price_minor"]:
        lines.append(f"Цена: {summary['price_minor'] // 100} ₽")
    return "\n".join(lines)


def _revision_buttons():
    labels = {
        Revision.Category.FACE: "Лицо",
        Revision.Category.HAIR: "Волосы",
        Revision.Category.BODY: "Тело",
        Revision.Category.DETAIL: "Детали",
        Revision.Category.COLORS: "Цвета",
        Revision.Category.STYLE_EXPECTATION: "Стиль",
        Revision.Category.OTHER: "Другое",
    }
    return [[{"text": label, "payload": f"preview_revision:{value}"}] for value, label in labels.items()]


def _handle_callback(event: MaxEvent, *, adapter, client):
    identity = adapter.get_or_create_identity(event.user)
    payload = event.callback_payload

    if payload == "menu:main":
        _send_main_menu(event, client=client)
    elif payload == "menu:order":
        _send_products(event, adapter=adapter, client=client)
    elif payload == "menu:prices":
        _reply(client, event, text=prices_text(telegram=False))
    elif payload == "menu:examples":
        _reply(client, event, text=EXAMPLES_TEXT)
    elif payload == "menu:photos":
        _reply(client, event, text=PHOTO_REQUIREMENTS)
    elif payload == "menu:how":
        _reply(client, event, text=ORDER_HOW_IT_WORKS)
    elif payload == "menu:contact":
        _reply(client, event, text=CONTACT_TEXT)
    elif payload.startswith("product:"):
        product_code = payload.split(":", 1)[1]
        if not adapter.active_products().filter(code=product_code).exists():
            raise MaxFlowError("Product is unavailable")
        buttons = [
            [{"text": style.name, "payload": f"style:{product_code}:{style.code}"}]
            for style in adapter.active_styles()
        ]
        _reply(client, event, text="Выберите стиль", buttons=buttons)
    elif payload.startswith("style:"):
        _, product_code, style_code = payload.split(":", 2)
        order = adapter.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        if product_requires_custom_phrases(order.product):
            _reply(client, event, text=PHOTO_PROMPT)
        elif adapter.required_emotion_count(product=order.product):
            text, buttons = _emotion_step(adapter, order)
            _reply(client, event, text=text, buttons=buttons)
        else:
            _reply(client, event, text=PHOTO_PROMPT)
    elif payload.startswith("emotion:"):
        adapter.select_emotion(identity=identity, emotion_code=payload.split(":", 1)[1])
        _reply(client, event, text=PHOTO_PROMPT)
    elif payload == "emotions:confirm":
        adapter.confirm_emotions(identity=identity)
        _reply(client, event, text=PHOTO_PROMPT)
    elif payload == "photos_done":
        # Validate photos/selection now (same errors as before) but stay in
        # AWAITING_PHOTOS: checkout is reachable only through consent:accept.
        order = adapter.photos_ready(identity)
        if order.photos.count() == 1:
            # soft reminder only — one photo is accepted (DRF-2090)
            _reply(client, event, text=SINGLE_PHOTO_REMINDER)
        if product_requires_custom_phrases(order.product):
            adapter.set_awaiting_input(identity=identity, value="phrases")
            _reply(client, event, text="Напишите 9 желаемых фраз — по одной в каждой строке.")
        elif product_requires_customer_contact(order.product):
            adapter.set_awaiting_input(identity=identity, value="contact")
            _reply(client, event, text="Оставьте имя и удобный способ связи: @username, телефон или ссылку.")
        else:
            _reply(client, event, text=CONSENT_TEXT, buttons=CONSENT_BUTTONS)
    elif payload == "order:confirm":
        order = adapter.current_photo_order(identity)
        if not adapter.flow.customer_contact_complete(order):
            raise MaxFlowError("A valid contact is required")
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
        PreviewFeedbackService.request_revision(
            order=_feedback_order(identity),
            category=category,
        )
        _reply(
            client,
            event,
            text="Правка принята. Мы подготовим обновлённое превью.",
        )

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
