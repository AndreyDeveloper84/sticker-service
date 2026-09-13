from __future__ import annotations

from apps.core.models import ChannelIdentity
from apps.core.services.preview_delivery import DeliveryResult
from apps.telegram_bot.client import TelegramBotClient


class TelegramPreviewDeliveryAdapter:
    channel = ChannelIdentity.Channel.TELEGRAM

    def __init__(self, *, client: TelegramBotClient):
        self.client = client

    def send_preview(
        self,
        *,
        recipient_id: str,
        content: bytes,
        mime_type: str,
        filename: str,
        caption: str,
    ) -> DeliveryResult:
        message = self.client.send_photo(
            chat_id=recipient_id,
            content=content,
            filename=filename,
            mime_type=mime_type,
            caption=caption,
        )
        controls = self.client.send_message(
            chat_id=recipient_id,
            text="Как вам превью?",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Нравится", "callback_data": "preview_approve"},
                    {"text": "Нужно исправить", "callback_data": "preview_revision"},
                ]]
            },
        )
        return DeliveryResult(
            message_id=str((message or {}).get("message_id") or ""),
            metadata={
                "chat_id": str((message or {}).get("chat", {}).get("id") or recipient_id),
                "controls_message_id": str((controls or {}).get("message_id") or ""),
            },
        )
