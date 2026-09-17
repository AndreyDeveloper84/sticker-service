from __future__ import annotations

from apps.core.models import ChannelIdentity
from apps.core.services.preview_delivery import DeliveryResult
from apps.telegram_bot.client import TelegramBotClient


class TelegramFinalDeliveryAdapter:
    """Final set delivery over Telegram (DRF-2053).

    Each sticker goes out as a document (original PNG, no re-encoding,
    transparency preserved), followed by one "set is ready" message.
    Sticker-set creation (uploadStickerFile / createNewStickerSet) is a
    separate owner decision and is intentionally not done here.
    """

    channel = ChannelIdentity.Channel.TELEGRAM

    def __init__(self, *, client: TelegramBotClient):
        self.client = client

    def send_final_item(
        self,
        *,
        recipient_id: str,
        content: bytes,
        mime_type: str,
        filename: str,
        caption: str,
        index: int,
        total: int,
    ) -> DeliveryResult:
        message = self.client.send_document(
            chat_id=recipient_id,
            content=content,
            filename=filename,
            mime_type=mime_type,
            caption=caption,
        )
        return DeliveryResult(
            message_id=str((message or {}).get("message_id") or ""),
            metadata={
                "chat_id": str((message or {}).get("chat", {}).get("id") or recipient_id),
                "index": index,
                "total": total,
            },
        )

    def send_final_summary(self, *, recipient_id: str, text: str, total: int) -> DeliveryResult:
        message = self.client.send_message(chat_id=recipient_id, text=text)
        return DeliveryResult(
            message_id=str((message or {}).get("message_id") or ""),
            metadata={
                "chat_id": str((message or {}).get("chat", {}).get("id") or recipient_id),
                "total": total,
            },
        )
