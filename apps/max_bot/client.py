import json
from uuid import uuid4
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from apps.max_bot.client_payment_link import render_button


class MaxBotClient:
    base_url = "https://platform-api2.max.ru"

    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, path: str, *, query=None, body=None):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}

    def send_message(self, *, user_id: str, text: str, buttons=None, attachments=None):
        message_attachments = list(attachments or [])
        if buttons:
            message_attachments.append(
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [render_button(button) for button in row]
                            for row in buttons
                        ]
                    },
                }
            )
        body = {"text": text}
        if message_attachments:
            body["attachments"] = message_attachments
        return self._request("POST", "/messages", query={"user_id": user_id}, body=body)

    def _upload_image(self, *, content: bytes, filename: str, mime_type: str) -> str:
        init = self._request("POST", "/uploads", query={"type": "image"}, body={})
        upload_url = str(init.get("url") or "")
        if not upload_url.startswith("https://"):
            raise RuntimeError("MAX upload URL is missing")

        boundary = f"----sticker-service-{uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="data"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = Request(
            upload_url,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urlopen(request, timeout=30) as response:
            raw = response.read()
        uploaded = json.loads(raw.decode("utf-8")) if raw else {}

        token = str(
            uploaded.get("token")
            or uploaded.get("retval", {}).get("token")
            or init.get("token")
            or ""
        )
        if not token:
            query = parse_qs(urlparse(upload_url).query)
            token = str((query.get("token") or [""])[0])
        if not token:
            raise RuntimeError("MAX image upload returned no token")
        return token

    def send_image(self, *, user_id: str, content: bytes, filename: str, mime_type: str, caption: str = ""):
        token = self._upload_image(
            content=content,
            filename=filename,
            mime_type=mime_type,
        )
        return self.send_message(
            user_id=user_id,
            text=caption,
            attachments=[{"type": "image", "payload": {"token": token}}],
        )

    def answer_callback(self, *, callback_id: str):
        return self._request("POST", "/answers", query={"callback_id": callback_id}, body={})

    def download(self, url: str) -> bytes:
        with urlopen(url, timeout=15) as response:
            return response.read()
