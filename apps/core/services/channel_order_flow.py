from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.media import MediaService
from apps.core.services.order_state import OrderStateService


class ChannelFlowError(ValueError):
    pass


def product_emotion_count(product: Product) -> int:
    """Emotions required by the product; 0 means the product has no emotion step."""
    try:
        return int((product.config or {}).get("emotion_count") or 0)
    except (TypeError, ValueError):
        return 0


def product_emotion_options(product: Product) -> list[dict]:
    """Deterministic emotion catalog offered by the product: [{"code", "label"}, ...]."""
    raw = (product.config or {}).get("emotions") or []
    options = []
    for item in raw:
        if isinstance(item, dict) and item.get("code"):
            options.append({"code": str(item["code"]), "label": str(item.get("label") or item["code"])})
    return options


def order_emotion_codes(order: Order) -> list[str]:
    """Emotion codes selected for the order so far."""
    selection = order.selection or {}
    return [str(code) for code in selection.get("emotions") or []]


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
            selection={"emotions": []} if product_emotion_count(product) else {},
        )
        return OrderStateService.transition(order=order, to_status=Order.Status.AWAITING_PHOTOS)

    def required_emotion_count(self, *, product: Product) -> int:
        return product_emotion_count(product)

    def emotion_options(self, *, product: Product) -> list[dict]:
        return product_emotion_options(product)

    @transaction.atomic
    def select_emotion(self, *, identity, emotion_code: str) -> Order:
        """Add one emotion to the in-progress order (single-emotion products)."""
        order = self.current_photo_order(identity)
        required = product_emotion_count(order.product)
        options = product_emotion_options(order.product)
        if required <= 0 or not options:
            raise ChannelFlowError("This product has no emotion selection")
        emotion_code = str(emotion_code)
        if emotion_code not in {option["code"] for option in options}:
            raise ChannelFlowError("Unknown emotion for this product")
        selected = order_emotion_codes(order)
        if emotion_code in selected:
            raise ChannelFlowError("Emotion is already selected")
        if len(selected) >= required:
            raise ChannelFlowError("All required emotions are already selected")
        order.selection = {"emotions": selected + [emotion_code]}
        order.save(update_fields=["selection", "updated_at"])
        return order

    @transaction.atomic
    def confirm_emotions(self, *, identity) -> Order:
        """Accept the product's full deterministic emotion set (sticker packs)."""
        order = self.current_photo_order(identity)
        required = product_emotion_count(order.product)
        codes = [option["code"] for option in product_emotion_options(order.product)]
        if required <= 0 or not codes:
            raise ChannelFlowError("This product has no emotion selection")
        if len(codes) != required:
            raise ChannelFlowError("Product emotion set does not match the required count")
        order.selection = {"emotions": codes}
        order.save(update_fields=["selection", "updated_at"])
        return order

    @staticmethod
    def selection_complete(order: Order) -> bool:
        required = product_emotion_count(order.product)
        return required <= 0 or len(order_emotion_codes(order)) == required

    def order_summary(self, order: Order) -> dict:
        """Channel-agnostic checkout summary; adapters only format it for display."""
        product = order.product
        config = product.config or {}
        labels = {option["code"]: option["label"] for option in product_emotion_options(product)}
        codes = order_emotion_codes(order)
        try:
            quantity = int(config.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0

        def _price(key):
            try:
                value = int(config.get(key))
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        return {
            "product_code": product.code,
            "product_name": product.name,
            "style_name": order.style.name,
            "quantity": quantity,
            "emotion_count": product_emotion_count(product),
            "emotion_codes": codes,
            "emotions": [labels.get(code, code) for code in codes],
            "price_minor": _price("price_minor"),
            "price_stars": _price("price_stars"),
            "currency": str(config.get("currency") or "RUB").upper(),
        }

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
        if not self.selection_complete(order):
            raise ChannelFlowError("Emotion selection is not complete")
        return OrderStateService.transition(order=order, to_status=Order.Status.READY_FOR_CHECKOUT)
