from django.contrib import admin
from django.urls import include, path

from apps.max_bot.provider_webhook import payment_webhook as max_payment_webhook
from apps.max_bot.views import webhook as max_webhook
from apps.telegram_bot.views import webhook as telegram_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("apps.core.urls")),
    path("telegram/webhook/", telegram_webhook, name="telegram-webhook"),
    path("max/webhook/", max_webhook, name="max-webhook"),
    path("max/payment/webhook/", max_payment_webhook, name="max-payment-webhook"),
]
