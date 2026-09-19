import os

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from apps.core.console_html import lines_html
from apps.core.console_text import PREVIEW_ALREADY_SENT
from apps.core.models import GeneratedAsset, GenerationJob, Order
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.preview_delivery import PreviewDeliveryError, PreviewDeliveryService
from apps.max_bot.client import MaxBotClient
from apps.max_bot.preview_delivery import MaxPreviewDeliveryAdapter
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.preview_delivery import TelegramPreviewDeliveryAdapter


class PreviewDeliveryOrderAdmin(ProductionOrderAdmin):
    @admin.display(description="Превью")
    def preview_assets(self, order):
        if not order or not order.pk:
            return "—"
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by("-created_at")
        if not assets:
            return "Превью пока нет."

        rows = []
        for asset in assets:
            metadata = asset.metadata or {}
            approved = bool(metadata.get("internal_approved"))
            deliveries = list(metadata.get("deliveries") or [])
            sent = [item for item in deliveries if item.get("status") == "sent"]
            failed = [item for item in deliveries if item.get("status") == "failed"]
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            action = ""
            if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and not approved and not sent:
                # an already-sent preview cannot be sent again — approving it
                # would only lead to «уже отправлено»
                action = format_html(
                    ' · <a class="button" href="{}">Одобрить это превью</a>',
                    reverse("admin:core_order_approve_preview", args=[order.pk, asset.pk]),
                )
            elif order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and approved and not sent:
                action = format_html(
                    ' · <a class="button" href="{}">Отправить клиенту</a>',
                    reverse("admin:core_order_deliver_preview", args=[order.pk]),
                )
            delivery_text = ""
            if sent:
                latest = sent[-1]
                delivery_text = f" · ОТПРАВЛЕНО ({latest.get('channel')}, сообщение {latest.get('message_id') or '—'})"
            elif failed:
                latest = failed[-1]
                delivery_text = f" · СБОЙ ОТПРАВКИ: {(latest.get('metadata') or {}).get('error', 'неизвестная ошибка')}"
            customer = " · КЛИЕНТ ОДОБРИЛ" if metadata.get("customer_approved") else ""
            rows.append(
                format_html(
                    '#{} · попытка {} · <a href="{}" target="_blank" rel="noopener">Открыть</a>{}{}{}{}',
                    asset.pk,
                    asset.job.attempt,
                    open_url,
                    " · ОДОБРЕНО" if approved else "",
                    delivery_text,
                    customer,
                    action,
                )
            )
        return lines_html(rows)

    def get_urls(self):
        custom = [
            path(
                "<int:order_id>/deliver-preview/",
                self.admin_site.admin_view(self.deliver_preview_view),
                name="core_order_deliver_preview",
            )
        ]
        return custom + super().get_urls()

    def get_delivery_service(self, order):
        if order.channel_identity.channel == "telegram":
            adapter = TelegramPreviewDeliveryAdapter(
                client=TelegramBotClient(os.getenv("TELEGRAM_BOT_TOKEN", ""))
            )
        elif order.channel_identity.channel == "max":
            adapter = MaxPreviewDeliveryAdapter(
                client=MaxBotClient(os.getenv("MAX_BOT_TOKEN", ""))
            )
        else:
            raise PreviewDeliveryError("Unsupported delivery channel")
        return PreviewDeliveryService(adapter=adapter)

    def approve_preview_view(self, request, order_id, asset_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        try:
            asset = GeneratedAsset.objects.select_related("job").get(
                pk=asset_id,
                order=order,
                kind=GeneratedAsset.Kind.PREVIEW,
            )
        except GeneratedAsset.DoesNotExist as exc:
            raise Http404 from exc

        action_url = reverse("admin:core_order_approve_preview", args=[order.pk, asset.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Одобрить превью #{asset.pk}",
                action_url=action_url,
                detail="Превью будет отмечено как одобренное. Клиент увидит его только после нажатия «Отправить превью клиенту».",
            )
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(request, "Одобрение доступно только на внутренней проверке превью.", level=messages.ERROR)
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if asset.job.status != GenerationJob.Status.SUCCEEDED:
            self.message_user(request, "Нельзя одобрить превью от неуспешной генерации.", level=messages.ERROR)
            return redirect(reverse("admin:core_order_change", args=[order.pk]))

        for previous in order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW):
            metadata = dict(previous.metadata or {})
            changed = False
            for key in ("internal_approved", "internal_approved_at"):
                if key in metadata:
                    metadata.pop(key)
                    changed = True
            if changed:
                previous.metadata = metadata
                previous.save(update_fields=["metadata", "updated_at"])

        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        metadata["internal_approved_at"] = timezone.now().isoformat()
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])
        self.message_user(request, f"Превью #{asset.pk} одобрено. Теперь отправьте его клиенту.", level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def _already_delivered_hint(self, order, exc) -> str:
        """«уже отправлено» + which preview to approve instead (the newest
        successful one that has not been sent), so the operator is not left
        with a bare refusal after a customer revision."""
        if "already delivered" not in str(exc):
            return ""
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).select_related("job")
        for asset in assets.order_by("-created_at", "-pk"):
            if asset.job.status == GenerationJob.Status.SUCCEEDED and not self._preview_sent(asset):
                return (
                    f"{PREVIEW_ALREADY_SENT} — одобрите новое превью #{asset.pk} "
                    "(«Превью» → «Одобрить это превью»)."
                )
        return f"{PREVIEW_ALREADY_SENT} — новых превью нет, сначала «Перегенерировать превью»."

    def deliver_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_deliver_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Отправить превью клиенту",
                action_url=action_url,
                detail="Одобренное превью будет отправлено клиенту в канал заказа. После успешной отправки заказ перейдёт в «Ждём ответ клиента».",
            )
        try:
            asset = self.get_delivery_service(order).deliver(order=order)
        except PreviewDeliveryError as exc:
            hint = self._already_delivered_hint(order, exc)
            if hint:
                self.message_user(request, hint, level=messages.ERROR)
            else:
                self._fail(request, exc)
        else:
            self.message_user(request, f"Превью #{asset.pk} отправлено клиенту.", level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))


def install_preview_delivery_console():
    try:
        admin.site.unregister(Order)
    except admin.sites.NotRegistered:
        pass
    admin.site.register(Order, PreviewDeliveryOrderAdmin)
