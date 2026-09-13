from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.max_bot.payments import HttpExternalPaymentProvider, MaxExternalPaymentAdapter, MaxPaymentError

@csrf_exempt
def payment_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    adapter = MaxExternalPaymentAdapter(provider=HttpExternalPaymentProvider.from_env())
    try:
        payment = adapter.confirm_webhook(
            body=request.body,
            signature=request.headers.get("X-Payment-Signature", ""),
        )
    except MaxPaymentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "payment_id": payment.pk, "order_id": payment.order_id})
