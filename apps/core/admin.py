from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path

from .models import ChannelIdentity, Order, OrderPhoto, Product, Style, User
from .storage import LocalMediaStorage


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)


@admin.register(ChannelIdentity)
class ChannelIdentityAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "channel", "external_user_id", "username", "created_at")
    list_filter = ("channel",)
    search_fields = ("external_user_id", "username", "display_name")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Style)
class StyleAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


class OrderPhotoInline(admin.TabularInline):
    model = OrderPhoto
    extra = 0
    readonly_fields = ("storage_key", "original_filename", "mime_type", "size_bytes", "status", "created_at")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "channel_identity", "product", "style", "status", "created_at")
    list_filter = ("status", "channel_identity__channel", "product", "style")
    search_fields = ("channel_identity__external_user_id", "channel_identity__username")
    inlines = (OrderPhotoInline,)


@admin.register(OrderPhoto)
class OrderPhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "status", "mime_type", "size_bytes", "created_at")
    list_filter = ("status", "mime_type")
    search_fields = ("storage_key", "original_filename")

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
