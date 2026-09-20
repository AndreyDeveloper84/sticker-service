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

Uploads (``POST /uploads?type=...`` → upload URL → multipart ``data`` →
token → ``attachments[{type, payload: {token}}]``): ``image`` answers
``{"photos": {<key>: {"token"}}}``, ``file`` answers a flat ``{"token"}``.
A file is processed asynchronously — ``POST /messages`` right after the
upload may answer 400 ``attachment.not.ready``; the message is NOT created
then, so :meth:`MaxBotClient.send_file` retries with the same token and
finally raises :class:`MaxAttachmentNotReady` (retryable, no duplicate).
"""

import json
import os
import time
from uuid import uuid4

import httpx

from apps.max_bot.client_payment_link import render_button

DEFAULT_API_BASE = "https://botapi.max.ru"


def uploaded_image_token(uploaded) -> str:
    """Attachment token from the multipart upload answer of an image upload
    URL (``POST /uploads?type=image`` → upload URL → multipart POST).

    Real wire shape (measured on staging, synthetic PNG):
    ``{"photos": {"<key>": {"token": "<str>"}}}`` — one entry whose key is
    opaque; the token is the only inner field that matters. Returns "" when
    the answer carries no photos/token.
    """
    if not isinstance(uploaded, dict):
        return ""
    photos = uploaded.get("photos")
    if not isinstance(photos, dict):
        return ""
    for entry in photos.values():
        if isinstance(entry, dict) and entry.get("token"):
            return str(entry["token"])
    return ""


def uploaded_file_token(uploaded) -> str:
    """Attachment token from the multipart upload answer of a FILE upload URL
    (``POST /uploads?type=file`` → upload URL → multipart POST).

    Wire shape (dev.max.ru, official TS/Java clients): a flat
    ``{"token": "<str>"}`` — unlike images, no ``photos`` map. Returns ""
    when the answer carries no token.
    """
    if not isinstance(uploaded, dict):
        return ""
    token = uploaded.get("token")
    return str(token) if token else ""


ATTACHMENT_NOT_READY = "attachment.not.ready"
# seconds between retries of POST /messages while the uploaded file is still
# being processed (MAX answers 400 attachment.not.ready; the message is not
# created, the token stays valid): 0.5 → 1 → 2 s, then every 3 s, ≈30 s in
# total — the schedule of the official Java client.
FILE_ATTACHMENT_RETRY_DELAYS = (0.5, 1, 2) + (3,) * 9


def _sleep(seconds: float) -> None:  # patched in tests
    time.sleep(seconds)


def is_attachment_not_ready(exc: Exception) -> bool:
    """400 with ``{"code": "attachment.not.ready", ...}`` in the body."""
    if not isinstance(exc, MaxAPIError) or exc.status_code != 400:
        return False
    try:
        body = json.loads(exc.body or "")
    except ValueError:
        return ATTACHMENT_NOT_READY in (exc.body or "")
    return isinstance(body, dict) and body.get("code") == ATTACHMENT_NOT_READY


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


class MaxAttachmentNotReady(MaxAPIError):
    """``attachment.not.ready`` survived every retry: the file is uploaded,
    no message was created. Retryable for the delivery service — a later
    resend cannot duplicate anything."""

    retryable = True


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

    def edit_message(self, *, message_id: str, text: str | None = None, attachments=None):
        """PUT /messages?message_id=… — ``attachments=[]`` removes every
        attachment (incl. the inline keyboard); None leaves it unchanged."""
        body = {}
        if text is not None:
            body["text"] = text
        if attachments is not None:
            body["attachments"] = list(attachments)
        return self._request("PUT", "/messages", query={"message_id": message_id}, body=body)

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

    def _upload(self, *, upload_type: str, content: bytes, filename: str, mime_type: str) -> tuple[dict, dict]:
        """``POST /uploads?type=<upload_type>`` then the multipart POST of
        ``content`` to the returned URL. Returns (init answer, upload answer);
        token extraction depends on the type (see the callers)."""
        init = self._request("POST", "/uploads", query={"type": upload_type}, body={})
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
        return init, uploaded

    def _upload_image(self, *, content: bytes, filename: str, mime_type: str) -> str:
        init, uploaded = self._upload(upload_type="image", content=content, filename=filename, mime_type=mime_type)
        token = uploaded_image_token(uploaded)
        if not token:
            # legacy / defensive fallbacks, never observed on the live API
            token = str(
                uploaded.get("token")
                or (uploaded.get("retval") or {}).get("token")
                or init.get("token")
                or ""
            )
        if not token:
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(str(init.get("url") or "")).query)
            token = str((query.get("token") or [""])[0])
        if not token:
            raise MaxAPIError(0, "MAX image upload returned no token")
        return token

    def _upload_file(self, *, content: bytes, filename: str, mime_type: str) -> str:
        _init, uploaded = self._upload(upload_type="file", content=content, filename=filename, mime_type=mime_type)
        token = uploaded_file_token(uploaded)
        if not token:
            raise MaxAPIError(0, "MAX file upload returned no token")
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

    def send_file(self, *, user_id: str, content: bytes, filename: str, mime_type: str, caption: str = ""):
        """Send ``content`` as a FILE attachment — byte-for-byte, the client
        can save it (an ``image`` attachment is re-encoded by MAX and loses
        the PNG alpha channel, which makes a sticker unusable).

        ``POST /messages`` is retried while MAX answers 400
        ``attachment.not.ready`` (the file is still being processed; no
        message was created, the token stays valid). After the last delay
        the error propagates — the caller must not count it as sent.
        """
        token = self._upload_file(content=content, filename=filename, mime_type=mime_type)
        attachments = [{"type": "file", "payload": {"token": token}}]
        for delay in (*FILE_ATTACHMENT_RETRY_DELAYS, None):
            try:
                return self.send_message(user_id=user_id, text=caption, attachments=attachments)
            except MaxAPIError as exc:
                if not is_attachment_not_ready(exc):
                    raise
                if delay is None:
                    raise MaxAttachmentNotReady(exc.status_code, exc.body) from exc
            _sleep(delay)
