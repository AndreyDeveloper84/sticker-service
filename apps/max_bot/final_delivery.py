from apps.core.models import ChannelIdentity
from apps.max_bot.client import created_message_id
from apps.core.services.final_delivery import SUMMARY_TEXT
from apps.core.services.preview_delivery import DeliveryResult

# DRF-2163: MAX has no bot API for sticker sets — the customer builds the
# set from the delivered PNGs in the «Стикеры в MAX» bot.
MAX_STICKERS_INSTRUCTION = (
    "Как сделать из них стикеры. Файл прозрачный — белый фон только в превью. "
    "Сохраните файл → бот «Стикеры в MAX» → «Создать набор» → загрузите PNG "
    "(нужен Цифровой ID)."
)
MAX_SUMMARY_TEXT = f"{SUMMARY_TEXT}\n\n{MAX_STICKERS_INSTRUCTION}"


def _message_id(response) -> str:
    return created_message_id(response)


class MaxFinalDeliveryAdapter:
    """Final set delivery over MAX (DRF-2053): one FILE message per sticker
    (the original PNG with its alpha channel — an ``image`` attachment is
    re-encoded by MAX onto a white background and cannot be used as a
    sticker; previews stay images), then one "set is ready" text message.
    At-most-once per slot and resume live in FinalDeliveryService and are
    untouched."""

    channel = ChannelIdentity.Channel.MAX
    summary_text = MAX_SUMMARY_TEXT

    def __init__(self, *, client):
        self.client = client

    def send_final_item(
        self, *, recipient_id, content, mime_type, filename, caption, index, total
    ):
        message = self.client.send_file(
            user_id=recipient_id,
            content=content,
            filename=filename,
            mime_type=mime_type,
            caption=caption,
        )
        return DeliveryResult(
            message_id=_message_id(message),
            metadata={"user_id": recipient_id, "index": index, "total": total},
        )

    def send_final_summary(self, *, recipient_id, text, total):
        message = self.client.send_message(user_id=recipient_id, text=text)
        return DeliveryResult(
            message_id=_message_id(message),
            metadata={"user_id": recipient_id, "total": total},
        )
