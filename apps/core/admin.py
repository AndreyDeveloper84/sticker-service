from django.contrib import admin

from .models import ChannelIdentity, Order, OrderPhoto, Product, Style, User


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
