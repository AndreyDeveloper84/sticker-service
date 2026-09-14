"""MAX webhook payload parser for Sticker Service.

Translates raw MAX webhook JSON into a small :class:`MaxEvent` DTO so
``views.py`` never digs through nested raw dicts. Field layout follows the
reference parser (``ai-bot-platform/apps/channels/max/parser.py``), without
the platform's CanonicalEvent/stream infrastructure.

Supported update types: ``bot_started``, ``message_created``,
``message_callback``. ``bot_started`` is the channel-level ``/start`` and
arrives with synthetic ``text="/start"``. Anything malformed raises
:class:`MaxParseError` instead of a bare KeyError deep in the views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MaxParseError(Exception):
    """Malformed or unsupported MAX webhook payload."""


@dataclass(frozen=True)
class MaxEvent:
    update_type: str
    user_id: str
    chat_id: str
    message_id: str = ""
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    callback_id: str = ""
    callback_payload: str = ""
    user: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_max_event(payload: dict[str, Any]) -> MaxEvent:
    """Parse a MAX webhook update into a :class:`MaxEvent`.

    Raises:
      MaxParseError: unsupported ``update_type`` or missing required fields.
    """
    if not isinstance(payload, dict):
        raise MaxParseError("MAX webhook payload is not a JSON object")

    update_type = payload.get("update_type")
    if update_type == "bot_started":
        return _parse_bot_started(payload)
    if update_type == "message_created":
        return _parse_message_created(payload)
    if update_type == "message_callback":
        return _parse_message_callback(payload)
    raise MaxParseError(
        f"Unsupported MAX update_type={update_type!r}. Supported: "
        "'bot_started', 'message_created', 'message_callback'."
    )


def _parse_bot_started(payload: dict[str, Any]) -> MaxEvent:
    """``bot_started`` — the channel-level /start (user tapped «Начать»)."""
    user = payload.get("user")
    if not isinstance(user, dict) or "user_id" not in user:
        raise MaxParseError("MAX bot_started payload missing required field: user.user_id")

    chat_id = payload.get("chat_id")
    if chat_id is None:
        raise MaxParseError("MAX bot_started payload missing required field: chat_id")

    deeplink = payload.get("payload") or ""
    text = f"/start {deeplink}".strip() if deeplink else "/start"

    return MaxEvent(
        update_type="bot_started",
        user_id=str(user["user_id"]),
        chat_id=str(chat_id),
        text=text,
        user=user,
        raw=payload,
    )


def _parse_message_created(payload: dict[str, Any]) -> MaxEvent:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise MaxParseError("MAX payload missing required field: message")

    sender = message.get("sender")
    if not isinstance(sender, dict) or "user_id" not in sender:
        raise MaxParseError("MAX payload missing required field: message.sender.user_id")

    recipient = message.get("recipient")
    if not isinstance(recipient, dict) or "chat_id" not in recipient:
        raise MaxParseError("MAX payload missing required field: message.recipient.chat_id")

    body = message.get("body") or {}
    text = body.get("text") or ""
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []

    return MaxEvent(
        update_type="message_created",
        user_id=str(sender["user_id"]),
        chat_id=str(recipient["chat_id"]),
        message_id=str(body.get("mid") or body.get("seq") or ""),
        text=text,
        attachments=attachments,
        user=sender,
        raw=payload,
    )


def _parse_message_callback(payload: dict[str, Any]) -> MaxEvent:
    callback = payload.get("callback")
    if not isinstance(callback, dict):
        raise MaxParseError("MAX callback payload missing required field: callback")

    user = callback.get("user")
    if not isinstance(user, dict) or "user_id" not in user:
        raise MaxParseError("MAX callback payload missing required field: callback.user.user_id")

    callback_id = str(callback.get("callback_id") or "")
    if not callback_id:
        raise MaxParseError("MAX callback payload missing required field: callback.callback_id")

    # chat_id/message id live on the ORIGINAL message the keyboard was
    # attached to — the bot's own earlier reply.
    message = payload.get("message") or {}
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id")
    if chat_id is None:
        raise MaxParseError("MAX callback payload missing required field: message.recipient.chat_id")

    body = message.get("body") or {}

    return MaxEvent(
        update_type="message_callback",
        user_id=str(user["user_id"]),
        chat_id=str(chat_id),
        message_id=str(body.get("mid") or body.get("seq") or ""),
        text=str(callback.get("payload") or ""),
        callback_id=callback_id,
        callback_payload=str(callback.get("payload") or ""),
        user=user,
        raw=payload,
    )
