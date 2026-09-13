from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Order, OrderPhoto
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

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "user",
            "channel_identity",
            "product",
            "style",
        ).prefetch_related("photos")


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
