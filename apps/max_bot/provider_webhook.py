from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

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
        adapter.confirm_webhook(body=request.body, signature="")
    except MaxPaymentIgnored as exc:
        return JsonResponse({"ok": True, "ignored": exc.reason})
    except MaxPaymentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True})
