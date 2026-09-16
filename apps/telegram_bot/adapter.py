from apps.core.models import ChannelIdentity
from apps.core.services.channel_order_flow import ChannelFlowError, ChannelOrderFlowService


TelegramFlowError = ChannelFlowError


class TelegramAdapter:
    def __init__(self, *, media_service=None):
        self.flow = ChannelOrderFlowService(media_service=media_service)

    def get_or_create_identity(self, telegram_user: dict) -> ChannelIdentity:
        display_name = " ".join(
            part
            for part in [telegram_user.get("first_name", ""), telegram_user.get("last_name", "")]
            if part
        )
        return self.flow.get_or_create_identity(
            channel=ChannelIdentity.Channel.TELEGRAM,
            external_user_id=str(telegram_user["id"]),
            username=telegram_user.get("username") or "",
            display_name=display_name,
        )

    def active_products(self):
        return self.flow.active_products()

    def active_styles(self):
        return self.flow.active_styles()

    def create_or_get_order(self, *, identity, product_code: str, style_code: str):
        return self.flow.create_or_get_order(
            identity=identity,
            product_code=product_code,
            style_code=style_code,
        )

    def required_emotion_count(self, *, product):
        return self.flow.required_emotion_count(product=product)

    def emotion_options(self, *, product):
        return self.flow.emotion_options(product=product)

    def select_emotion(self, *, identity, emotion_code: str):
        return self.flow.select_emotion(identity=identity, emotion_code=emotion_code)

    def confirm_emotions(self, *, identity):
        return self.flow.confirm_emotions(identity=identity)

    def order_summary(self, order):
        return self.flow.order_summary(order)

    def current_photo_order(self, identity):
        return self.flow.current_photo_order(identity)

    def save_photo_bytes(self, *, identity, content: bytes, filename: str, mime_type: str):
        return self.flow.save_photo_bytes(
            identity=identity,
            content=content,
            filename=filename,
            mime_type=mime_type,
        )

    def complete_photos(self, identity):
        return self.flow.complete_photos(identity)
