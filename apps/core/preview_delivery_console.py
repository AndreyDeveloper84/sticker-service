import os

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from apps.core.console_html import lines_html
from apps.core.models import GeneratedAsset, GenerationJob, Order
from apps.core.production_console import ProductionOrderAdmin
from apps.core.services.preview_delivery import PreviewDeliveryError, PreviewDeliveryService
from apps.max_bot.client import MaxBotClient
from apps.max_bot.preview_delivery import MaxPreviewDeliveryAdapter
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.preview_delivery import TelegramPreviewDeliveryAdapter


class PreviewDeliveryOrderAdmin(ProductionOrderAdmin):
    @admin.display(description="Preview assets")
    def preview_assets(self, order):
        if not order or not order.pk:
            return "—"
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by("-created_at")
        if not assets:
            return "Preview assets пока нет."

        rows = []
        for asset in assets:
            metadata = asset.metadata or {}
            approved = bool(metadata.get("internal_approved"))
            deliveries = list(metadata.get("deliveries") or [])
            sent = [item for item in deliveries if item.get("status") == "sent"]
            failed = [item for item in deliveries if item.get("status") == "failed"]
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            action = ""
            if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and not approved:
                action = format_html(
                    ' · <a class="button" href="{}">Approve this preview</a>',
                    reverse("admin:core_order_approve_preview", args=[order.pk, asset.pk]),
                )
            elif order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and approved and not sent:
                action = format_html(
                    ' · <a class="button" href="{}">Deliver preview</a>',
                    reverse("admin:core_order_deliver_preview", args=[order.pk]),
                )
            delivery_text = ""
            if sent:
                latest = sent[-1]
                delivery_text = f" · SENT {latest.get('channel')} message={latest.get('message_id') or '—'}"
            elif failed:
                latest = failed[-1]
                delivery_text = f" · DELIVERY FAILED: {(latest.get('metadata') or {}).get('error', 'unknown error')}"
            rows.append(
                format_html(
                    '#{} · attempt {} · <a href="{}" target="_blank" rel="noopener">Открыть preview</a>{}{}{}',
                    asset.pk,
                    asset.job.attempt,
                    open_url,
                    " · APPROVED" if approved else "",
                    delivery_text,
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
                title=f"Approve preview #{asset.pk}",
                action_url=action_url,
                detail="Asset будет отмечен как одобренный. PREVIEW_REVIEW начнётся только после успешной доставки клиенту.",
            )
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(request, "Approve доступен только на internal preview review.", level=messages.ERROR)
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if asset.job.status != GenerationJob.Status.SUCCEEDED:
            self.message_user(request, "Нельзя одобрить asset от неуспешного generation job.", level=messages.ERROR)
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
        self.message_user(request, f"Preview #{asset.pk} approved. Deliver it to start customer review.", level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def deliver_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_deliver_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Deliver approved preview",
                action_url=action_url,
                detail="Одобренный preview будет отправлен клиенту через исходный канал. После успешной отправки заказ перейдёт в PREVIEW_REVIEW.",
            )
        try:
            asset = self.get_delivery_service(order).deliver(order=order)
        except PreviewDeliveryError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, f"Preview #{asset.pk} delivered successfully.", level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))


def install_preview_delivery_console():
    try:
        admin.site.unregister(Order)
    except admin.sites.NotRegistered:
        pass
    admin.site.register(Order, PreviewDeliveryOrderAdmin)
