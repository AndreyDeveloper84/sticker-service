"""Telegram Bot API outbound client.

Single transport source of truth for everything Sticker Service sends to
Telegram: JSON calls, multipart uploads (sendPhoto) and file downloads all
share the same origin/proxy configuration.

### Configurability (DRF-1870)

The staging VPS cannot reach ``api.telegram.org`` directly (TCP timeout to
149.154.166.110:443, IPv6 unreachable — measured 2026-09-15 from the
backend container). The transport is therefore configurable via
constructor args, Django settings or env:

- ``TELEGRAM_API_ORIGIN`` — default ``https://api.telegram.org``; a Bot API
  compatible relay/base goes here (``{origin}/bot{token}/{method}``).
- ``TELEGRAM_FILE_ORIGIN`` — file downloads; defaults to the API origin
  (``{origin}/file/bot{token}/{file_path}``).
- ``TELEGRAM_PROXY_URL`` — optional outbound proxy applied to ALL Telegram
  traffic (JSON calls, multipart, downloads). Never hardcode credentials.

Defaults without overrides are production-compatible (direct api.telegram.org).

### Error contract

:class:`TelegramAPIError` distinguishes network/timeout (status_code=0),
HTTP non-2xx, and Telegram ``ok=false``. The bot token is part of every
request URL, so exception messages and logs NEVER include URLs or
``str(httpx_exception)`` (httpx embeds the URL) — only the method name,
status and Telegram's own ``description``.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_ORIGIN = "https://api.telegram.org"


class TelegramAPIError(Exception):
    """Telegram transport failure: network/timeout, HTTP non-2xx, or ok=false."""

    def __init__(self, method: str, *, status_code: int = 0, description: str = ""):
        self.method = method
        self.status_code = status_code
        self.description = description
        # No URL, no token — method name and Telegram's own description only.
        super().__init__(f"telegram api error method={method} status={status_code}: {description[:200]}")


def _setting_or_env(name: str) -> str:
    try:
        from django.conf import settings

        value = getattr(settings, name, "")
        if value:
            return str(value)
    except Exception:  # settings not configured (plain scripts)
        pass
    return os.getenv(name, "")


# Process-wide keep-alive clients, one per proxy configuration. A module-level
# ``httpx.request`` per call would pay a cold TCP+TLS handshake every time —
# the exact failure mode measured for MAX outbound on staging. Gunicorn sync
# workers are separate processes, so each lazily builds its own client.
_SHARED_CLIENTS: dict[str, httpx.Client] = {}


def _shared_client(proxy_url: str, timeout: float) -> httpx.Client:
    key = proxy_url or ""
    client = _SHARED_CLIENTS.get(key)
    if client is None:
        client = httpx.Client(proxy=proxy_url or None, timeout=timeout)
        _SHARED_CLIENTS[key] = client
    return client


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        *,
        api_origin: str | None = None,
        file_origin: str | None = None,
        proxy_url: str | None = None,
        timeout: float = 30.0,
    ):
        if not token:
            raise ValueError("Telegram bot token is required")
        self.token = token
        self.api_origin = (api_origin or _setting_or_env("TELEGRAM_API_ORIGIN") or DEFAULT_API_ORIGIN).rstrip("/")
        self.file_origin = (file_origin or _setting_or_env("TELEGRAM_FILE_ORIGIN") or self.api_origin).rstrip("/")
        self.proxy_url = proxy_url if proxy_url is not None else _setting_or_env("TELEGRAM_PROXY_URL")
        self.timeout = timeout

    # ------------------------------------------------------------------ HTTP

    def _client(self) -> httpx.Client:
        return _shared_client(self.proxy_url, self.timeout)

    def _api_url(self, method: str) -> str:
        return f"{self.api_origin}/bot{self.token}/{method}"

    def _unwrap(self, method: str, response: httpx.Response):
        if response.status_code >= 400:
            logger.warning("telegram.api_error method=%s status=%s", method, response.status_code)
            raise TelegramAPIError(method, status_code=response.status_code, description="http error")
        try:
            data = response.json()
        except ValueError:
            logger.warning("telegram.api_error method=%s status=%s non-json", method, response.status_code)
            raise TelegramAPIError(method, status_code=response.status_code, description="invalid json") from None
        if not data.get("ok"):
            description = str(data.get("description") or "")[:200]
            logger.warning("telegram.api_error method=%s ok=false desc=%r", method, description)
            raise TelegramAPIError(method, status_code=response.status_code, description=description or "ok=false")
        return data.get("result")

    def _post(self, method: str, payload: dict):
        try:
            response = self._client().post(
                self._api_url(method),
                json=payload,
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            # NOTE: str(exc) embeds the URL, which contains the token — log
            # and raise with the exception TYPE only.
            logger.warning("telegram.network_error method=%s exc=%s", method, type(exc).__name__)
            raise TelegramAPIError(method, description=f"network: {type(exc).__name__}") from exc
        return self._unwrap(method, response)

    def _post_multipart(self, method: str, *, fields: dict, file_field: str, filename: str, content: bytes, mime_type: str):
        try:
            response = self._client().post(
                self._api_url(method),
                data={key: str(value) for key, value in fields.items()},
                files={file_field: (filename, content, mime_type)},
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            logger.warning("telegram.network_error method=%s exc=%s", method, type(exc).__name__)
            raise TelegramAPIError(method, description=f"network: {type(exc).__name__}") from exc
        return self._unwrap(method, response)

    # --------------------------------------------------------------- methods

    def get_me(self):
        return self._post("getMe", {})

    def send_message(self, *, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def send_photo(self, *, chat_id, content: bytes, filename: str, mime_type: str, caption: str = ""):
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        return self._post_multipart(
            "sendPhoto",
            fields=fields,
            file_field="photo",
            filename=filename,
            content=content,
            mime_type=mime_type,
        )

    def send_invoice(self, *, chat_id, title, description, payload, amount_stars):
        # Telegram Stars: currency XTR, no provider_token.
        return self._post(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": title,
                "description": description,
                "payload": payload,
                "currency": "XTR",
                "prices": [{"label": title, "amount": amount_stars}],
            },
        )

    def answer_pre_checkout_query(self, *, pre_checkout_query_id, ok, error_message=None):
        payload = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if error_message:
            payload["error_message"] = error_message
        return self._post("answerPreCheckoutQuery", payload)

    def answer_callback_query(self, *, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._post("answerCallbackQuery", payload)

    def get_file(self, file_id: str):
        return self._post("getFile", {"file_id": file_id})

    def download_file(self, file_path: str) -> bytes:
        url = f"{self.file_origin}/file/bot{self.token}/{file_path}"
        try:
            response = self._client().get(url, timeout=self.timeout)
        except httpx.RequestError as exc:
            logger.warning("telegram.network_error method=download_file exc=%s", type(exc).__name__)
            raise TelegramAPIError("download_file", description=f"network: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            logger.warning("telegram.api_error method=download_file status=%s", response.status_code)
            raise TelegramAPIError("download_file", status_code=response.status_code, description="http error")
        return response.content
