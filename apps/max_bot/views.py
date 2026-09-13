import json
import os
from mimetypes import guess_type
from urllib.parse import urlparse

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import Order, Revision
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.max_bot.adapter import MaxAdapter, MaxFlowError
from apps.max_bot.checkout import start_checkout
from apps.max_bot.client import MaxBotClient


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

    adapter = MaxAdapter()
    client = MaxBotClient(os.getenv("MAX_BOT_TOKEN", ""))
    try:
        _handle_update(update, adapter=adapter, client=client)
    except (MaxFlowError, PreviewFeedbackError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)

    return JsonResponse({"ok": True})


def _handle_update(update, *, adapter, client):
    update_type = update.get("update_type")
    if update_type == "bot_started":
        user = update["user"]
        identity = adapter.get_or_create_identity(user)
        return _send_products(identity.external_user_id, adapter=adapter, client=client)
    if update_type == "message_created":
        return _handle_message(update["message"], adapter=adapter, client=client)
    if update_type == "message_callback":
        return _handle_callback(update, adapter=adapter, client=client)
    return None


def _send_products(user_id, *, adapter, client):
    buttons = [[{"text": product.name, "payload": f"product:{product.code}"}] for product in adapter.active_products()]
    return client.send_message(user_id=user_id, text="Выберите продукт", buttons=buttons)


def _handle_message(message, *, adapter, client):
    sender = message["sender"]
    identity = adapter.get_or_create_identity(sender)
    body = message.get("body") or {}
    text = body.get("text") or ""

    if text.startswith("/start"):
        return _send_products(identity.external_user_id, adapter=adapter, client=client)

    for attachment in body.get("attachments") or []:
        if attachment.get("type") != "image":
            continue
        image_url = _image_url(attachment)
        if not image_url:
            continue
        content = client.download(image_url)
        filename = urlparse(image_url).path.rsplit("/", 1)[-1] or "photo.jpg"
        mime_type = guess_type(filename)[0] or "image/jpeg"
        adapter.save_photo_bytes(
            identity=identity,
            content=content,
            filename=filename,
            mime_type=mime_type,
        )
        return client.send_message(
            user_id=identity.external_user_id,
            text="Фото сохранено. Отправьте ещё или нажмите «Фото загружены».",
            buttons=[[{"text": "Фото загружены", "payload": "photos_done"}]],
        )
    return None


def _image_url(attachment):
    payload = attachment.get("payload") or {}
    if payload.get("url"):
        return payload["url"]
    photos = payload.get("photos") or {}
    if isinstance(photos, dict):
        candidates = [value.get("url") for value in photos.values() if isinstance(value, dict) and value.get("url")]
        return candidates[-1] if candidates else None
    if isinstance(photos, list):
        candidates = [item.get("url") for item in photos if isinstance(item, dict) and item.get("url")]
        return candidates[-1] if candidates else None
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


def _handle_callback(update, *, adapter, client):
    user = update.get("user") or (update.get("message") or {}).get("sender")
    identity = adapter.get_or_create_identity(user)
    callback = update["callback"]
    payload = callback.get("payload") or ""

    if payload.startswith("product:"):
        product_code = payload.split(":", 1)[1]
        if not adapter.active_products().filter(code=product_code).exists():
            raise MaxFlowError("Product is unavailable")
        buttons = [
            [{"text": style.name, "payload": f"style:{product_code}:{style.code}"}]
            for style in adapter.active_styles()
        ]
        client.send_message(user_id=identity.external_user_id, text="Выберите стиль", buttons=buttons)
    elif payload.startswith("style:"):
        _, product_code, style_code = payload.split(":", 2)
        adapter.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        client.send_message(
            user_id=identity.external_user_id,
            text="Отправьте несколько хороших фотографий человека.",
        )
    elif payload == "photos_done":
        adapter.complete_photos(identity)
        start_checkout(identity=identity, client=client)
    elif payload == "preview_approve":
        PreviewFeedbackService.approve(order=_feedback_order(identity))
        client.send_message(user_id=identity.external_user_id, text="Превью принято. Спасибо!")
    elif payload == "preview_revision":
        client.send_message(
            user_id=identity.external_user_id,
            text="Что нужно исправить? Выберите основную причину.",
            buttons=_revision_buttons(),
        )
    elif payload.startswith("preview_revision:"):
        category = payload.split(":", 1)[1]
        PreviewFeedbackService.request_revision(
            order=_feedback_order(identity),
            category=category,
        )
        client.send_message(
            user_id=identity.external_user_id,
            text="Правка принята. Мы подготовим обновлённое превью.",
        )

    callback_id = callback.get("callback_id")
    if callback_id:
        return client.answer_callback(callback_id=callback_id)
    return None
