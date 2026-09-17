import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.core.models import Order, Revision
from apps.core.services.channel_order_flow import PILOT_CONSENT_BUTTON_LABEL, PILOT_CONSENT_TEXT
from apps.core.services.preview_feedback import PreviewFeedbackError, PreviewFeedbackService
from apps.telegram_bot.adapter import TelegramAdapter, TelegramFlowError
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.paid_notice import notify_customer_paid
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter, configured_stars_price


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
    except (TelegramPaymentError, PreviewFeedbackError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)})
    except TelegramFlowError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)

    return JsonResponse({"ok": True})


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


def _handle_message(message, *, adapter, payment_adapter, client):
    identity = adapter.get_or_create_identity(message["from"])
    chat_id = message["chat"]["id"]

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
        buttons = [[{"text": p.name, "callback_data": f"product:{p.code}"}] for p in adapter.active_products()]
        return client.send_message(chat_id=chat_id, text="Выберите продукт", reply_markup={"inline_keyboard": buttons})

    photos = message.get("photo") or []
    if photos:
        file_info = client.get_file(photos[-1]["file_id"])
        file_path = file_info["file_path"]
        content = client.download_file(file_path)
        mime_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        adapter.save_photo_bytes(identity=identity, content=content, filename=file_path.rsplit("/", 1)[-1], mime_type=mime_type)
        return client.send_message(chat_id=chat_id, text="Фото сохранено. Отправьте ещё или нажмите «Фото загружены».", reply_markup={"inline_keyboard": [[{"text": "Фото загружены", "callback_data": "photos_done"}]]})


def _feedback_order(identity):
    order = identity.orders.filter(status=Order.Status.PREVIEW_REVIEW).order_by("-created_at").first()
    if not order:
        raise PreviewFeedbackError("Нет превью, ожидающего вашей оценки")
    return order


PHOTO_PROMPT = "Отправьте несколько хороших фотографий человека."

# Consent gate (parity with MAX, DRF-2069): shown after the photos are
# complete, before the order summary and the Stars invoice; the accepted text
# version is persisted on the order so checkout can fail closed without it.
CONSENT_TEXT = PILOT_CONSENT_TEXT
CONSENT_REPLY_MARKUP = {"inline_keyboard": [[{"text": PILOT_CONSENT_BUTTON_LABEL, "callback_data": "consent:accept"}]]}
PAY_REPLY_MARKUP = {"inline_keyboard": [[{"text": "Оплатить", "callback_data": "pay"}]]}


def _emotion_step(adapter, order):
    """Emotion step text + keyboard, driven entirely by Product.config.

    A product whose deterministic emotion set matches the required count
    (sticker pack) is confirmed as a whole; otherwise emotions are picked
    one by one (single sticker).
    """
    options = adapter.emotion_options(product=order.product)
    required = adapter.required_emotion_count(product=order.product)
    if required == len(options):
        labels = ", ".join(option["label"] for option in options)
        text = f"В набор входят {required} эмоций: {labels}."
        buttons = [[{"text": "Подтвердить набор", "callback_data": "emotions:confirm"}]]
    else:
        text = "Выберите эмоцию для стикера."
        buttons = [[{"text": option["label"], "callback_data": f"emotion:{option['code']}"}] for option in options]
    return text, {"inline_keyboard": buttons}


def _summary_text(summary, *, price_stars):
    lines = [f"Ваш заказ: {summary['product_name']}", f"Стиль: {summary['style_name']}"]
    if summary["quantity"]:
        lines.append(f"Стикеров: {summary['quantity']}")
    if summary["emotions"]:
        lines.append(f"Эмоции: {', '.join(summary['emotions'])}")
    if price_stars:
        lines.append(f"Цена: {price_stars} Stars")
    return "\n".join(lines)


def _summary_stars_price(product):
    # Shows exactly what the XTR invoice will charge; never a RUB amount.
    try:
        return configured_stars_price(product)
    except TelegramPaymentError:
        return None


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
    return [[{"text": label, "callback_data": f"preview_revision:{value}"}] for value, label in labels.items()]


def _handle_callback(callback, *, adapter, payment_adapter, client):
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
        order = adapter.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        if adapter.required_emotion_count(product=order.product):
            text, reply_markup = _emotion_step(adapter, order)
            client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        else:
            client.send_message(chat_id=chat_id, text=PHOTO_PROMPT)
    elif data.startswith("emotion:"):
        adapter.select_emotion(identity=identity, emotion_code=data.split(":", 1)[1])
        client.send_message(chat_id=chat_id, text=PHOTO_PROMPT)
    elif data == "emotions:confirm":
        adapter.confirm_emotions(identity=identity)
        client.send_message(chat_id=chat_id, text=PHOTO_PROMPT)
    elif data == "photos_done":
        # Validate photos/selection now (same errors as before) but stay in
        # AWAITING_PHOTOS: the invoice is reachable only through consent:accept.
        adapter.photos_ready(identity)
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
        PreviewFeedbackService.request_revision(
            order=_feedback_order(identity),
            category=category,
        )
        client.send_message(chat_id=chat_id, text="Правка принята. Мы подготовим обновлённое превью.")

    return client.answer_callback_query(callback_query_id=callback["id"])
