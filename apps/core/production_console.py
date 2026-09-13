from django.contrib import admin, messages
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .image_providers import OpenAIImageProvider
from .models import GeneratedAsset, GenerationJob, Order, OrderPhoto
from .services.generation import GenerationError, GenerationService
from .services.order_state import InvalidOrderTransition, OrderStateService
from .storage import LocalMediaStorage


class ProductionOrderPhotoInline(admin.TabularInline):
    model = OrderPhoto
    extra = 0
    can_delete = False
    fields = (
        "open_photo",
        "original_filename",
        "mime_type",
        "size_bytes",
        "status",
        "created_at",
    )
    readonly_fields = fields

    @admin.display(description="Фото")
    def open_photo(self, photo):
        if not photo.pk:
            return "—"
        url = reverse("admin:core_orderphoto_file", args=[photo.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener">Открыть фото</a>', url)

    def has_add_permission(self, request, obj=None):
        return False


class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "channel",
        "channel_identity",
        "product",
        "style",
        "status",
        "photo_count",
        "created_at",
    )
    list_filter = ("status", "channel_identity__channel", "product", "style")
    search_fields = (
        "=id",
        "channel_identity__external_user_id",
        "channel_identity__username",
        "channel_identity__display_name",
    )
    list_select_related = ("user", "channel_identity", "product", "style")
    readonly_fields = (
        "user",
        "channel_identity",
        "product",
        "style",
        "status",
        "customer_notes",
        "preview_controls",
        "generation_history",
        "preview_assets",
        "created_at",
        "updated_at",
    )
    fields = (
        "user",
        "channel_identity",
        "product",
        "style",
        "status",
        "customer_notes",
        "operator_notes",
        "preview_controls",
        "generation_history",
        "preview_assets",
        "created_at",
        "updated_at",
    )
    inlines = (ProductionOrderPhotoInline,)
    ordering = ("-created_at",)

    @admin.display(description="Канал", ordering="channel_identity__channel")
    def channel(self, order):
        return order.channel_identity.get_channel_display()

    @admin.display(description="Фото")
    def photo_count(self, order):
        return order.photos.count()

    @admin.display(description="Preview — действия")
    def preview_controls(self, order):
        if not order or not order.pk:
            return "—"
        links = []
        if order.status in {Order.Status.PAID, Order.Status.PREVIEW_GENERATING}:
            links.append(
                (
                    reverse("admin:core_order_generate_preview", args=[order.pk]),
                    "Generate / Retry Preview",
                )
            )
        if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW:
            links.append(
                (
                    reverse("admin:core_order_regenerate_preview", args=[order.pk]),
                    "Regenerate Preview",
                )
            )
        if not links:
            return "Нет доступных действий для текущего статуса."
        return format_html_join(
            " &nbsp; ",
            '<a class="button" href="{}">{}</a>',
            links,
        )

    @admin.display(description="Generation jobs")
    def generation_history(self, order):
        if not order or not order.pk:
            return "—"
        jobs = order.generation_jobs.order_by("-attempt", "-created_at")
        if not jobs:
            return "Generation jobs пока нет."
        return format_html_join(
            "<br>",
            "<span>attempt {} · {} · {} {}</span>",
            (
                (
                    job.attempt,
                    job.get_status_display(),
                    job.provider,
                    f"· {job.error}" if job.error else "",
                )
                for job in jobs
            ),
        )

    @admin.display(description="Preview assets")
    def preview_assets(self, order):
        if not order or not order.pk:
            return "—"
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by(
            "-created_at"
        )
        if not assets:
            return "Preview assets пока нет."

        rows = []
        for asset in assets:
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            approved = bool((asset.metadata or {}).get("internal_approved"))
            approve_link = ""
            if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and not approved:
                approve_url = reverse(
                    "admin:core_order_approve_preview",
                    args=[order.pk, asset.pk],
                )
                approve_link = format_html(
                    ' · <a class="button" href="{}">Approve this preview</a>',
                    approve_url,
                )
            rows.append(
                format_html(
                    '#{} · attempt {} · <a href="{}" target="_blank" rel="noopener">Открыть preview</a>{}{}',
                    asset.pk,
                    asset.job.attempt,
                    open_url,
                    " · APPROVED" if approved else "",
                    approve_link,
                )
            )
        return format_html_join("<br>", "{}", ((row,) for row in rows))

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("user", "channel_identity", "product", "style")
            .prefetch_related("photos", "generation_jobs", "generated_assets__job")
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:order_id>/generate-preview/",
                self.admin_site.admin_view(self.generate_preview_view),
                name="core_order_generate_preview",
            ),
            path(
                "<int:order_id>/regenerate-preview/",
                self.admin_site.admin_view(self.regenerate_preview_view),
                name="core_order_regenerate_preview",
            ),
            path(
                "<int:order_id>/approve-preview/<int:asset_id>/",
                self.admin_site.admin_view(self.approve_preview_view),
                name="core_order_approve_preview",
            ),
            path(
                "preview-asset/<int:asset_id>/file/",
                self.admin_site.admin_view(self.preview_asset_file_view),
                name="core_preview_asset_file",
            ),
        ]
        return custom + urls

    def get_generation_service(self):
        return GenerationService(provider=OpenAIImageProvider())

    def _confirmation(self, request, *, order, title, action_url, detail):
        return TemplateResponse(
            request,
            "admin/core/order/preview_action_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": title,
                "order": order,
                "action_url": action_url,
                "detail": detail,
                "opts": self.model._meta,
            },
        )

    def generate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_generate_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Generate preview",
                action_url=action_url,
                detail="Будет запущена новая preview attempt через настроенный image provider.",
            )
        try:
            asset = self.get_generation_service().generate_preview(order=order)
        except GenerationError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Preview #{asset.pk} generated and is ready for internal review.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def regenerate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_regenerate_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Regenerate preview",
                action_url=action_url,
                detail="Предыдущие assets сохранятся. Будет создана новая generation attempt.",
            )
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Regenerate доступен только на internal preview review.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PREVIEW_GENERATING,
            )
            asset = self.get_generation_service().generate_preview(order=order)
        except (InvalidOrderTransition, GenerationError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"New preview #{asset.pk} generated; previous assets were preserved.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

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

        action_url = reverse(
            "admin:core_order_approve_preview",
            args=[order.pk, asset.pk],
        )
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Approve preview #{asset.pk}",
                action_url=action_url,
                detail="Именно этот asset будет отмечен как внутренне одобренный для отправки клиенту.",
            )

        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Approve доступен только на internal preview review.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if asset.job.status != GenerationJob.Status.SUCCEEDED:
            self.message_user(
                request,
                "Нельзя одобрить asset от неуспешного generation job.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))

        for previous in order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW):
            metadata = dict(previous.metadata or {})
            changed = False
            if "internal_approved" in metadata:
                metadata.pop("internal_approved", None)
                changed = True
            if "internal_approved_at" in metadata:
                metadata.pop("internal_approved_at", None)
                changed = True
            if changed:
                previous.metadata = metadata
                previous.save(update_fields=["metadata", "updated_at"])

        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        metadata["internal_approved_at"] = timezone.now().isoformat()
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PREVIEW_REVIEW,
            )
        except InvalidOrderTransition as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Preview #{asset.pk} approved for customer review.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def preview_asset_file_view(self, request, asset_id):
        try:
            asset = GeneratedAsset.objects.get(pk=asset_id)
        except GeneratedAsset.DoesNotExist as exc:
            raise Http404 from exc
        storage = LocalMediaStorage()
        if not storage.exists(asset.storage_key):
            raise Http404
        return FileResponse(
            storage.open(asset.storage_key),
            filename=f"preview-{asset.pk}.png",
            content_type=asset.mime_type or "application/octet-stream",
        )


class ProductionOrderPhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "status", "mime_type", "size_bytes", "created_at")
    list_filter = ("status", "mime_type")
    search_fields = ("storage_key", "original_filename")
    readonly_fields = (
        "order",
        "storage_key",
        "original_filename",
        "mime_type",
        "size_bytes",
        "status",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:photo_id>/file/",
                self.admin_site.admin_view(self.file_view),
                name="core_orderphoto_file",
            )
        ]
        return custom + urls

    def file_view(self, request, photo_id):
        try:
            photo = OrderPhoto.objects.get(pk=photo_id)
        except OrderPhoto.DoesNotExist as exc:
            raise Http404 from exc
        storage = LocalMediaStorage()
        if not storage.exists(photo.storage_key):
            raise Http404
        return FileResponse(
            storage.open(photo.storage_key),
            filename=photo.original_filename or f"photo-{photo.pk}",
            content_type=photo.mime_type or "application/octet-stream",
        )


def install_production_console():
    for model in (Order, OrderPhoto):
        try:
            admin.site.unregister(model)
        except admin.sites.NotRegistered:
            pass
    admin.site.register(Order, ProductionOrderAdmin)
    admin.site.register(OrderPhoto, ProductionOrderPhotoAdmin)
