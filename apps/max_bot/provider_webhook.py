import os

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.max_bot.client import MaxBotClient
from apps.max_bot.paid_notice import notify_customer_paid
from apps.max_bot.payments import MaxExternalPaymentAdapter, MaxPaymentError, MaxPaymentIgnored
from apps.max_bot.payments_yookassa import YooKassaPaymentProvider

@csrf_exempt
def payment_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    # YooKassa retries non-200 deliveries for up to 24h: ACK webhooks that are
    # valid but need no action, fail with 400 only on unverifiable payloads.
    try:
        adapter = MaxExternalPaymentAdapter(provider=YooKassaPaymentProvider.from_env())
        payment = adapter.confirm_webhook(body=request.body, signature="")
    except MaxPaymentIgnored as exc:
        return JsonResponse({"ok": True, "ignored": exc.reason})
    except MaxPaymentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    # PAID is committed above; the customer notice is best-effort and at most
    # once per payment (duplicate webhooks return here with nothing to send).
    notify_customer_paid(payment=payment, client=MaxBotClient(os.getenv("MAX_BOT_TOKEN", "")))
    return JsonResponse({"ok": True})
