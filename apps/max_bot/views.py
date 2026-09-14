import json
import logging
import os
from mimetypes import guess_type
from urllib.parse import urlparse

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

from apps.core.models import Order, Revision
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
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
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


def _handle_event(event: MaxEvent, *, adapter, client):
    if event.update_type == "bot_started":
        identity = adapter.get_or_create_identity(event.user)
        return _send_products(event, adapter=adapter, client=client)
    if event.update_type == "message_created":
        return _handle_message(event, adapter=adapter, client=client)
    if event.update_type == "message_callback":
        return _handle_callback(event, adapter=adapter, client=client)
    return None


def _send_products(event: MaxEvent, *, adapter, client):
    buttons = [[{"text": product.name, "payload": f"product:{product.code}"}] for product in adapter.active_products()]
    return _reply(client, event, text="Выберите продукт", buttons=buttons)


def _handle_message(event: MaxEvent, *, adapter, client):
    identity = adapter.get_or_create_identity(event.user)

    if event.text.startswith("/start"):
        return _send_products(event, adapter=adapter, client=client)

    for attachment in event.attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "image":
            continue
        image_url = extract_photo_url(attachment)
        if not image_url:
            continue
        try:
            content = download_photo(image_url)
        except PhotoTooLargeError:
            return _reply(client, event, text="Фото слишком большое (больше 10 МБ). Отправьте файл поменьше.")
        except PhotoDownloadError:
            return _reply(client, event, text="Не удалось загрузить фото. Отправьте его ещё раз.")
        filename = urlparse(image_url).path.rsplit("/", 1)[-1] or "photo.jpg"
        mime_type = guess_type(filename)[0] or "image/jpeg"
        adapter.save_photo_bytes(
            identity=identity,
            content=content,
            filename=filename,
            mime_type=mime_type,
        )
        return _reply(
            client,
            event,
            text="Фото сохранено. Отправьте ещё или нажмите «Фото загружены».",
            buttons=[[{"text": "Фото загружены", "payload": "photos_done"}]],
        )
    return None


def _feedback_order(identity):
    order = identity.orders.filter(status=Order.Status.PREVIEW_REVIEW).order_by("-created_at").first()
    if not order:
        raise PreviewFeedbackError("Нет превью, ожидающего вашей оценки")
    return order


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

    if payload.startswith("product:"):
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
        adapter.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        _reply(
            client,
            event,
            text="Отправьте несколько хороших фотографий человека.",
        )
    elif payload == "photos_done":
        adapter.complete_photos(identity)
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
    if event.callback_id:
        return client.answer_callback(callback_id=event.callback_id)
    return None
