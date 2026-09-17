from apps.core.models import ChannelIdentity
from apps.max_bot.client import created_message_id
from apps.core.services.preview_delivery import DeliveryResult


def _message_id(response) -> str:
    return created_message_id(response)


class MaxFinalDeliveryAdapter:
    """Final set delivery over MAX (DRF-2053): one image message per
    sticker (upload + send, like the preview adapter), then one "set is
    ready" text message."""

    channel = ChannelIdentity.Channel.MAX

    def __init__(self, *, client):
        self.client = client

    def send_final_item(
        self, *, recipient_id, content, mime_type, filename, caption, index, total
    ):
        message = self.client.send_image(
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
