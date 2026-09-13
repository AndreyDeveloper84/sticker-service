from apps.core.models import ChannelIdentity
from apps.core.services.preview_delivery import DeliveryResult


class MaxPreviewDeliveryAdapter:
    channel = ChannelIdentity.Channel.MAX

    def __init__(self, *, client):
        self.client = client

    def send_preview(self, *, recipient_id, content, mime_type, filename, caption):
        message = self.client.send_image(
            user_id=recipient_id,
            content=content,
            filename=filename,
            mime_type=mime_type,
            caption=caption,
        )
        controls = self.client.send_message(
            user_id=recipient_id,
            text="Как вам превью?",
            buttons=[[{
                "text": "Нравится",
                "payload": "preview_approve",
            }, {
                "text": "Нужно исправить",
                "payload": "preview_revision",
            }]],
        )
        message_id = (
            (message or {}).get("body", {}).get("mid")
            or (message or {}).get("message", {}).get("mid")
            or (message or {}).get("mid")
            or ""
        )
        controls_id = (
            (controls or {}).get("body", {}).get("mid")
            or (controls or {}).get("message", {}).get("mid")
            or (controls or {}).get("mid")
            or ""
        )
        return DeliveryResult(
            message_id=str(message_id),
            metadata={"user_id": recipient_id, "controls_message_id": str(controls_id)},
        )
