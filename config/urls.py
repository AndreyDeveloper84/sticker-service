from django.contrib import admin
from django.urls import include, path

from apps.telegram_bot.views import webhook as telegram_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("apps.core.urls")),
    path("telegram/webhook/", telegram_webhook, name="telegram-webhook"),
]
