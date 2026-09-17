"""MAX REST outbound client for Sticker Service.

Wire contract follows the proven reference implementation
(``ai-bot-platform/apps/channels/max/outbound.py``):

- API base: ``https://botapi.max.ru`` (override via Django setting or env
  ``MAX_API_BASE`` — tests/staging only).
- Authorization header carries the RAW bot token, not ``Bearer <token>``.
- ``POST /messages`` takes the recipient as a QUERY parameter
  (``chat_id=...`` or ``user_id=...``), never in the JSON body.
- ``POST /answers?callback_id=...`` acknowledges an inline-keyboard callback.

Addressing rule (measured in the reference deployment): ``chat_id``
identifies a dialog and is correct when replying to an inbound event from
that dialog; ``user_id`` identifies the person and works for
bot-initiated sends. Exactly one of them must be passed.

Error contract: network failures, timeouts, HTTP >= 400 all raise
:class:`MaxAPIError`; a 2xx with a non-JSON body returns ``{}``. No silent
failures.
"""

import os
from uuid import uuid4

import httpx

from apps.max_bot.client_payment_link import render_button

DEFAULT_API_BASE = "https://botapi.max.ru"


def created_message_id(response) -> str:
    """``mid`` of the message created by ``POST /messages`` (or ``/uploads``
    + send), or "" when the envelope carries none.

    Real wire shape (reference deployment, ``ai-bot-platform`` outbound):
    ``{"message": {"sender": …, "recipient": …, "timestamp": …,
    "body": {"mid": "…", "seq": …, "text": …}}}`` → ``message.body.mid``
    (``seq`` as a fallback). Older fixtures used ``body.mid`` /
    ``message.mid`` / top-level ``mid``; they stay accepted so no evidence
    silently degrades.
    """
    if not isinstance(response, dict):
        return ""
    message = response.get("message")
    body = message.get("body") if isinstance(message, dict) else None
    if isinstance(body, dict) and (body.get("mid") or body.get("seq")):
        return str(body.get("mid") or body.get("seq"))
    for candidate in (
        (response.get("body") or {}).get("mid") if isinstance(response.get("body"), dict) else None,
        message.get("mid") if isinstance(message, dict) else None,
        response.get("mid"),
    ):
        if candidate:
            return str(candidate)
    return ""


class MaxAPIError(Exception):
    """Non-2xx response from the MAX REST API, or a network failure."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"MAX API status={status_code}: {body[:200]}")


def _api_base() -> str:
    try:
        from django.conf import settings

        configured = getattr(settings, "MAX_API_BASE", "")
        if configured:
            return str(configured).rstrip("/")
    except Exception:  # settings not configured (e.g. plain scripts)
        pass
    return os.getenv("MAX_API_BASE", DEFAULT_API_BASE).rstrip("/")


_SHARED_CLIENT: httpx.Client | None = None


def _shared_client() -> httpx.Client:
    """Process-wide httpx client with keep-alive.

    A cold TCP connect to botapi.max.ru from the staging VPS costs ~5 s
    (measured 2026-09-14); a module-level ``httpx.request`` pays it on EVERY
    send, which under load exceeded the webhook-time budget and caused MAX
    to retry deliveries (duplicate messages). Connection reuse makes warm
    sends ~0.1 s. Gunicorn sync workers are separate processes, so each
    worker lazily gets its own client — no cross-thread sharing concerns
    beyond what httpx already handles.
    """
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = httpx.Client()
    return _SHARED_CLIENT


class MaxBotClient:
    def __init__(self, token: str, *, timeout: float = 30.0, api_base: str | None = None):
        self.token = token
        self.timeout = timeout
        self.base_url = (api_base or _api_base()).rstrip("/")

    # ------------------------------------------------------------------ HTTP

    def _headers(self) -> dict:
        return {
            "Authorization": self.token,  # raw token, NOT Bearer
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, query=None, body=None):
        url = f"{self.base_url}{path}"
        try:
            response = _shared_client().request(
                method,
                url,
                params=query,
                json=body if body is not None else {},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            # connection refused, DNS failure, timeout, ...
            raise MaxAPIError(0, str(exc)) from exc

        if response.status_code >= 400:
            raise MaxAPIError(response.status_code, response.text)

        try:
            return response.json()
        except ValueError:
            return {}

    # ----------------------------------------------------------------- sends

    def send_message(self, *, chat_id=None, user_id=None, text: str, buttons=None, attachments=None):
        """POST /messages. Exactly one of chat_id/user_id must be given."""
        if (chat_id is None) == (user_id is None):
            raise ValueError(
                "send_message needs exactly one of chat_id= (reply inside the "
                "dialog an event arrived from) or user_id= (write to a person "
                f"first); got chat_id={chat_id!r} user_id={user_id!r}"
            )

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

        query = {"chat_id": chat_id} if user_id is None else {"user_id": user_id}
        return self._request("POST", "/messages", query=query, body=body)

    def answer_callback(self, *, callback_id: str, notification: str = ""):
        """ACK an inline-keyboard callback: POST /answers?callback_id=...

        The current MAX contract rejects an empty body with
        400 ``proto.payload`` ("`message` or `notification` required"),
        measured on staging 2026-09-14 — so a body with ``notification``
        is always sent (empty string = no toast).
        """
        return self._request(
            "POST",
            "/answers",
            query={"callback_id": callback_id},
            body={"notification": notification},
        )

    # -------------------------------------------------------------- uploads

    def _upload_image(self, *, content: bytes, filename: str, mime_type: str) -> str:
        init = self._request("POST", "/uploads", query={"type": "image"}, body={})
        upload_url = str(init.get("url") or "")
        if not upload_url.startswith("https://"):
            raise MaxAPIError(0, "MAX upload URL is missing or not HTTPS")

        boundary = f"----sticker-service-{uuid4().hex}"
        payload = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="data"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        try:
            response = _shared_client().post(
                upload_url,
                content=payload,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise MaxAPIError(0, str(exc)) from exc
        if response.status_code >= 400:
            raise MaxAPIError(response.status_code, response.text)
        try:
            uploaded = response.json()
        except ValueError:
            uploaded = {}

        token = str(
            uploaded.get("token")
            or uploaded.get("retval", {}).get("token")
            or init.get("token")
            or ""
        )
        if not token:
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(upload_url).query)
            token = str((query.get("token") or [""])[0])
        if not token:
            raise MaxAPIError(0, "MAX image upload returned no token")
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
