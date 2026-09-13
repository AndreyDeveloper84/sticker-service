from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.media import MediaService
from apps.core.services.order_state import OrderStateService


class ChannelFlowError(ValueError):
    pass


class ChannelOrderFlowService:
    def __init__(self, *, media_service=None):
        self.media_service = media_service or MediaService()

    @transaction.atomic
    def get_or_create_identity(
        self,
        *,
        channel: str,
        external_user_id: str,
        username: str = "",
        display_name: str = "",
    ) -> ChannelIdentity:
        identity = (
            ChannelIdentity.objects.select_related("user")
            .filter(channel=channel, external_user_id=str(external_user_id))
            .first()
        )
        if identity:
            changed = False
            if identity.username != username:
                identity.username = username
                changed = True
            if identity.display_name != display_name:
                identity.display_name = display_name
                changed = True
            if changed:
                identity.save(update_fields=["username", "display_name", "updated_at"])
            return identity

        user = User.objects.create()
        return ChannelIdentity.objects.create(
            user=user,
            channel=channel,
            external_user_id=str(external_user_id),
            username=username,
            display_name=display_name,
        )

    def active_products(self):
        return Product.objects.filter(is_active=True).order_by("id")

    def active_styles(self):
        return Style.objects.filter(is_active=True).order_by("id")

    @transaction.atomic
    def create_or_get_order(self, *, identity, product_code: str, style_code: str) -> Order:
        product = Product.objects.filter(code=product_code, is_active=True).first()
        style = Style.objects.filter(code=style_code, is_active=True).first()
        if not product or not style:
            raise ChannelFlowError("Product or style is unavailable")

        existing = (
            Order.objects.filter(
                channel_identity=identity,
                status=Order.Status.AWAITING_PHOTOS,
            )
            .order_by("-id")
            .first()
        )
        if existing:
            if existing.product_id == product.id and existing.style_id == style.id:
                return existing
            raise ChannelFlowError("Another order is already waiting for photos")

        order = Order.objects.create(
            user=identity.user,
            channel_identity=identity,
            product=product,
            style=style,
        )
        return OrderStateService.transition(order=order, to_status=Order.Status.AWAITING_PHOTOS)

    def current_photo_order(self, identity) -> Order:
        order = (
            Order.objects.filter(
                channel_identity=identity,
                status=Order.Status.AWAITING_PHOTOS,
            )
            .order_by("-id")
            .first()
        )
        if not order:
            raise ChannelFlowError("No order is waiting for photos")
        return order

    def save_photo_bytes(self, *, identity, content: bytes, filename: str, mime_type: str):
        order = self.current_photo_order(identity)
        uploaded = SimpleUploadedFile(filename, content, content_type=mime_type)
        return self.media_service.save_order_photo(
            order=order,
            file=uploaded,
            original_filename=filename,
            mime_type=mime_type,
        )

    @transaction.atomic
    def complete_photos(self, identity) -> Order:
        order = self.current_photo_order(identity)
        if not order.photos.exists():
            raise ChannelFlowError("At least one photo is required")
        return OrderStateService.transition(order=order, to_status=Order.Status.READY_FOR_CHECKOUT)
