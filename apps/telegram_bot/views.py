import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.telegram_bot.adapter import TelegramAdapter, TelegramFlowError
from apps.telegram_bot.client import TelegramBotClient


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
    client = TelegramBotClient(settings.TELEGRAM_BOT_TOKEN)

    try:
        _handle_update(update, adapter=adapter, client=client)
    except TelegramFlowError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)

    return JsonResponse({"ok": True})


def _handle_update(update, *, adapter, client):
    if "message" in update:
        return _handle_message(update["message"], adapter=adapter, client=client)
    if "callback_query" in update:
        return _handle_callback(update["callback_query"], adapter=adapter, client=client)
    return None


def _handle_message(message, *, adapter, client):
    identity = adapter.get_or_create_identity(message["from"])
    chat_id = message["chat"]["id"]

    if (message.get("text") or "").startswith("/start"):
        buttons = [[{"text": p.name, "callback_data": f"product:{p.code}"}] for p in adapter.active_products()]
        return client.send_message(chat_id=chat_id, text="Выберите продукт", reply_markup={"inline_keyboard": buttons})

    photos = message.get("photo") or []
    if photos:
        file_info = client.get_file(photos[-1]["file_id"])
        file_path = file_info["file_path"]
        content = client.download_file(file_path)
        mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        adapter.save_photo_bytes(
            identity=identity,
            content=content,
            filename=file_path.rsplit("/", 1)[-1],
            mime_type=mime_type,
        )
        return client.send_message(
            chat_id=chat_id,
            text="Фото сохранено. Отправьте ещё или нажмите «Фото загружены».",
            reply_markup={"inline_keyboard": [[{"text": "Фото загружены", "callback_data": "photos_done"}]]},
        )


def _handle_callback(callback, *, adapter, client):
    identity = adapter.get_or_create_identity(callback["from"])
    chat_id = callback["message"]["chat"]["id"]
    data = callback.get("data") or ""

    if data.startswith("product:"):
        product_code = data.split(":", 1)[1]
        if not adapter.active_products().filter(code=product_code).exists():
            raise TelegramFlowError("Product is unavailable")
        buttons = [[{"text": s.name, "callback_data": f"style:{product_code}:{s.code}"}] for s in adapter.active_styles()]
        client.send_message(chat_id=chat_id, text="Выберите стиль", reply_markup={"inline_keyboard": buttons})
    elif data.startswith("style:"):
        _, product_code, style_code = data.split(":", 2)
        adapter.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        client.send_message(chat_id=chat_id, text="Отправьте несколько хороших фотографий человека.")
    elif data == "photos_done":
        adapter.complete_photos(identity)
        client.send_message(chat_id=chat_id, text="Фотографии приняты. Заказ готов к следующему шагу.")

    return client.answer_callback_query(callback_query_id=callback["id"])
