from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from apps.core.models import ChannelIdentity, Order, Product, Style, User
from apps.core.services.media import MediaService
from apps.core.services.order_state import OrderStateService


class ChannelFlowError(ValueError):
    pass


# Identifier of the customer consent text shown before checkout. Bump it when
# the wording changes so accepted orders stay auditable against the text the
# customer actually saw (Order.consent_version).
PILOT_CONSENT_VERSION = "pilot-2026-09-v1"

# Customer-facing consent text for PILOT_CONSENT_VERSION (same wording in every
# channel): rights to the photos, processing for the ordered stickers,
# service/order terms. Channel views only add their own button widget.
PILOT_CONSENT_TEXT = (
    "Перед оплатой подтвердите:\n"
    "• у вас есть право использовать загруженные фотографии;\n"
    "• фотографии будут обработаны для создания заказанных стикеров;\n"
    "• вы принимаете условия сервиса и заказа.\n\n"
    "Нажмите «Принимаю», чтобы перейти к оплате."
)
PILOT_CONSENT_BUTTON_LABEL = "Принимаю"


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


def product_requires_custom_phrases(product: Product) -> bool:
    return bool((product.config or {}).get("requires_custom_phrases"))


def product_requires_customer_contact(product: Product) -> bool:
    return bool((product.config or {}).get("requires_customer_contact"))


def order_custom_phrases(order: Order) -> list[str]:
    return [str(value) for value in (order.selection or {}).get("custom_phrases") or []]


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

    @transaction.atomic
    def change_order_choice(self, *, identity, product_code: str, style_code: str) -> Order:
        """Bot «⬅️ Назад» → a different product/style for the SAME in-progress
        order (AWAITING_PHOTOS only). Photos and the customer contact are
        kept; a product change resets emotions / custom phrases / awaiting
        input so the new product's steps are asked again. Orders past
        AWAITING_PHOTOS are never touched."""
        product = Product.objects.filter(code=product_code, is_active=True).first()
        style = Style.objects.filter(code=style_code, is_active=True).first()
        if not product or not style:
            raise ChannelFlowError("Product or style is unavailable")
        order = (
            Order.objects.select_for_update()
            .filter(channel_identity=identity, status=Order.Status.AWAITING_PHOTOS)
            .order_by("-id")
            .first()
        )
        if order is None:
            return self.create_or_get_order(identity=identity, product_code=product_code, style_code=style_code)
        selection = dict(order.selection or {})
        if order.product_id != product.id:
            selection.pop("custom_phrases", None)
            selection["emotions"] = []
            if not product_emotion_count(product):
                selection.pop("emotions", None)
        selection.pop("awaiting_input", None)
        order.product = product
        order.style = style
        order.selection = selection
        order.save(update_fields=["product", "style", "selection", "updated_at"])
        return order

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
        if product_requires_custom_phrases(order.product):
            raise ChannelFlowError("This product requires custom phrases")
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
        if product_requires_custom_phrases(order.product):
            return len(order_custom_phrases(order)) == required
        return required <= 0 or len(order_emotion_codes(order)) == required

    @transaction.atomic
    def save_custom_phrases(self, *, identity, text: str) -> Order:
        """Store the nine customer phrases in the canonical production slots.

        One phrase per line is deliberately required: it maps unambiguously to
        one generated sticker and prevents a long paragraph becoming a single
        unusable prompt.
        """
        order = self.current_photo_order(identity)
        if not product_requires_custom_phrases(order.product):
            raise ChannelFlowError("This product does not accept custom phrases")
        phrases = [line.strip() for line in str(text).splitlines() if line.strip()]
        required = product_emotion_count(order.product)
        if len(phrases) != required:
            raise ChannelFlowError(f"Exactly {required} custom phrases are required")
        selection = dict(order.selection or {})
        selection["emotions"] = [option["code"] for option in product_emotion_options(order.product)]
        selection["custom_phrases"] = phrases
        selection.pop("awaiting_input", None)
        order.selection = selection
        order.save(update_fields=["selection", "updated_at"])
        return order

    @transaction.atomic
    def set_awaiting_input(self, *, identity, value: str) -> Order:
        order = self.current_photo_order(identity)
        selection = dict(order.selection or {})
        selection["awaiting_input"] = value
        order.selection = selection
        order.save(update_fields=["selection", "updated_at"])
        return order

    def awaiting_input(self, identity) -> str:
        order = self.current_photo_order(identity)
        return str((order.selection or {}).get("awaiting_input") or "")

    @transaction.atomic
    def clear_awaiting_input(self, identity) -> Order | None:
        """Bot «⬅️ Назад» / «🏠 Главное меню»: stop waiting for free text on
        the in-progress order (no-op without one; never touches later statuses)."""
        order = (
            Order.objects.select_for_update()
            .filter(channel_identity=identity, status=Order.Status.AWAITING_PHOTOS)
            .order_by("-id")
            .first()
        )
        if order is None:
            return None
        selection = dict(order.selection or {})
        if selection.pop("awaiting_input", None) is not None:
            order.selection = selection
            order.save(update_fields=["selection", "updated_at"])
        return order

    def order_ready_to_confirm(self, identity) -> Order:
        """The order card / «✅ Подтвердить заказ» precondition: photos,
        emotions or phrases, and the customer contact are all in place."""
        order = self.current_photo_order(identity)
        if not order.photos.exists():
            raise ChannelFlowError("At least one photo is required")
        if not self.selection_complete(order):
            if product_requires_custom_phrases(order.product):
                raise ChannelFlowError("Custom phrases are not complete")
            raise ChannelFlowError("Emotion selection is not complete")
        if not self.customer_contact_complete(order):
            raise ChannelFlowError("A valid contact is required")
        return order

    @transaction.atomic
    def save_customer_contact(self, *, identity, text: str) -> Order:
        order = self.current_photo_order(identity)
        contact = " ".join(str(text).split())
        if len(contact) < 3 or len(contact) > 255:
            raise ChannelFlowError("A valid contact is required")
        selection = dict(order.selection or {})
        selection["contact"] = contact
        selection.pop("awaiting_input", None)
        order.selection = selection
        order.save(update_fields=["selection", "updated_at"])
        return order

    @staticmethod
    def customer_contact_complete(order: Order) -> bool:
        if not product_requires_customer_contact(order.product):
            return True
        return bool(str((order.selection or {}).get("contact") or "").strip())

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
            "emotions": order_custom_phrases(order) if product_requires_custom_phrases(product) else [labels.get(code, code) for code in codes],
            "contact": str((order.selection or {}).get("contact") or ""),
            "captioned": product_requires_custom_phrases(product),
            "price_minor": _price("price_minor"),
            "price_stars": _price("price_stars"),
            "currency": str(config.get("currency") or "RUB").upper(),
        }

    def current_photo_order_or_none(self, identity):
        return (
            Order.objects.filter(channel_identity=identity, status=Order.Status.AWAITING_PHOTOS)
            .order_by("-id")
            .first()
        )

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

    def assert_photos_complete(self, order: Order) -> None:
        """Photos + product selection are sufficient to leave AWAITING_PHOTOS."""
        if not order.photos.exists():
            raise ChannelFlowError("At least one photo is required")
        if not self.selection_complete(order):
            raise ChannelFlowError("Emotion selection is not complete")

    def photos_ready(self, identity) -> Order:
        """The in-progress order, validated but NOT transitioned (consent step)."""
        order = self.current_photo_order(identity)
        if not order.photos.exists():
            raise ChannelFlowError("At least one photo is required")
        # Phrases are collected after the photos for the custom product.
        # Standard products retain the existing emotion-before-photo gate.
        if not product_requires_custom_phrases(order.product) and not self.selection_complete(order):
            raise ChannelFlowError("Emotion selection is not complete")
        return order

    @transaction.atomic
    def accept_consent(self, *, identity, version: str = PILOT_CONSENT_VERSION) -> Order:
        """Persist the customer's consent on the order about to be checked out.

        Idempotent: a repeated accept on an order that already carries consent
        (whatever its later status) returns it unchanged. Consent is bound to
        one order; a new order never inherits it.
        """
        order = (
            Order.objects.select_for_update()
            .select_related("product")
            .filter(
                channel_identity=identity,
                status__in=[
                    Order.Status.AWAITING_PHOTOS,
                    Order.Status.READY_FOR_CHECKOUT,
                    Order.Status.AWAITING_PAYMENT,
                ],
            )
            .order_by("-id")
            .first()
        )
        if not order:
            raise ChannelFlowError("No order is waiting for consent")
        if order.consent_accepted:
            return order
        if order.status != Order.Status.AWAITING_PHOTOS:
            raise ChannelFlowError("Order is past the consent step without consent")
        self.assert_photos_complete(order)
        order.consent_version = str(version)
        order.consent_accepted_at = timezone.now()
        order.save(update_fields=["consent_version", "consent_accepted_at", "updated_at"])
        return order

    @transaction.atomic
    def complete_photos(self, identity) -> Order:
        order = self.current_photo_order(identity)
        self.assert_photos_complete(order)
        return OrderStateService.transition(order=order, to_status=Order.Status.READY_FOR_CHECKOUT)
