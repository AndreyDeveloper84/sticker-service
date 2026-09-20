"""Telegram sticker set for a delivered order (DRF-2163 Final Sticker UX).

After a successful FinalDelivery (every slot sent) the bot puts the final
PNGs into the CUSTOMER'S sticker set through the Bot API and sends the
``t.me/addstickers/<name>`` link:

- one set per Telegram customer: ``<prefix>_<user_id>_by_<botusername>``
  (``createNewStickerSet`` the first time, ``addStickerToSet`` for every
  later order — the set is looked up with ``getStickerSet``);
- static PNG, one side exactly 512 px (the finals already are), ≤ 512 KB —
  a bigger file is re-encoded (optimize, then a 256-colour palette) for the
  set only; the delivered file is untouched;
- emoji per sticker from the slot's emotion code (custom phrases → 🙂);
- at-most-once: the record lives in ``FinalDelivery.summary["sticker_set"]``
  of the completing run — every added slot keeps its ``file_id``, so a
  retry («Создать набор повторно») only adds the missing slots and never
  sends the link twice;
- any Bot API error is stored (``status: failed``, ``error``) and shown to
  the operator; the delivered files stay as they are.

The order status and the per-slot delivery semantics are not touched.
"""

from __future__ import annotations

import io
import logging
import re

from django.db import transaction
from django.utils import timezone
from PIL import Image

from apps.core.models import ChannelIdentity, FinalDelivery, Order
from apps.core.services.budget import setting
from apps.core.services.channel_order_flow import product_requires_custom_phrases
from apps.core.storage import LocalMediaStorage
from apps.telegram_bot.client import TelegramAPIError

logger = logging.getLogger(__name__)

STICKER_MAX_BYTES = 512 * 1024
DEFAULT_SET_PREFIX = "sticks"
NEUTRAL_EMOJI = "🙂"
EMOJI_BY_CODE = {
    "hello": "👋", "bye": "🙋", "thanks": "🙏", "great": "👍", "no": "🙅", "love": "❤️",
    "laugh": "😂", "angry": "😠", "surprised": "😮", "sad": "😢", "wow": "🤩", "ok": "👌",
    "think": "🤔", "sleep": "😴", "cool": "😎", "party": "🥳", "hug": "🤗", "wink": "😉",
}
LINK_TEXT = (
    "Ваш набор стикеров в Telegram: https://t.me/addstickers/{name}\n"
    "Нажмите ссылку и «Добавить стикеры» — набор появится в панели стикеров."
)


class StickerSetError(ValueError):
    pass


def bot_username(client) -> str:
    """``TELEGRAM_BOT_USERNAME`` (setting / env) or ``getMe`` — the set name
    must end with ``_by_<botusername>``."""
    configured = str(setting("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if configured:
        return configured
    me = client.get_me() or {}
    username = str(me.get("username") or "").strip()
    if not username:
        raise StickerSetError("Bot username is unknown (getMe returned none)")
    return username


def set_name(identity: ChannelIdentity, username: str) -> str:
    prefix = re.sub(r"[^a-z0-9]", "", str(setting("TELEGRAM_STICKER_SET_PREFIX") or DEFAULT_SET_PREFIX).lower())
    prefix = prefix or DEFAULT_SET_PREFIX
    if not prefix[0].isalpha():
        prefix = "s" + prefix
    user = re.sub(r"[^0-9]", "", str(identity.external_user_id)) or "0"
    return f"{prefix}_{user}_by_{username}"


def set_title(order: Order) -> str:
    from apps.core.bot_menu import product_title

    title = product_title(order.product) or "Стикеры"
    return title[:64]


def sticker_emoji(order: Order, slot_key: str) -> str:
    if product_requires_custom_phrases(order.product):
        return NEUTRAL_EMOJI
    return EMOJI_BY_CODE.get(str(slot_key), NEUTRAL_EMOJI)


def sticker_png(content: bytes) -> bytes:
    """The PNG as delivered when it fits 512 KB; otherwise a re-encoded copy
    (optimize → 256-colour palette). Raises when nothing fits."""
    if len(content) <= STICKER_MAX_BYTES:
        return content
    image = Image.open(io.BytesIO(content))
    image.load()
    for convert in (lambda im: im, lambda im: im.convert("RGBA").quantize(256, method=Image.Quantize.FASTOCTREE)):
        buffer = io.BytesIO()
        convert(image).save(buffer, format="PNG", optimize=True)
        if buffer.tell() <= STICKER_MAX_BYTES:
            return buffer.getvalue()
    raise StickerSetError("Sticker PNG exceeds 512 KB even after re-encoding")


def sticker_set_record(order: Order) -> dict | None:
    """The stored record from the latest run that has one, else None."""
    for run in order.final_deliveries.order_by("-attempt", "-pk"):
        record = (run.summary or {}).get("sticker_set")
        if record:
            return record
    return None


class TelegramStickerSetService:
    """``ensure(order)``: create/extend the customer's set with the order's
    final stickers and send the link — idempotent per slot and per link."""

    def __init__(self, *, client, storage=None):
        self.client = client
        self.storage = storage or LocalMediaStorage()

    def ensure(self, order: Order, *, actor_ref: str = "") -> dict:
        if order.channel_identity.channel != ChannelIdentity.Channel.TELEGRAM:
            raise StickerSetError("Sticker sets are created for Telegram orders only")
        if order.status != Order.Status.DELIVERED:
            raise StickerSetError("The set is created after the delivery is complete")
        run = order.final_deliveries.order_by("-attempt", "-pk").first()
        if run is None:
            raise StickerSetError("No delivery run found")
        record = dict(sticker_set_record(order) or {})
        identity = order.channel_identity
        user_id = identity.external_user_id
        stickers = dict(record.get("stickers") or {})  # slot_key → file_id (added to the set)
        try:
            username = record.get("bot_username") or bot_username(self.client)
            name = record.get("set_name") or set_name(identity, username)
            record.update({"bot_username": username, "set_name": name, "title": set_title(order)})
            exists = bool(record.get("set_exists")) or self._set_exists(name)
            for slot_key, asset in self._final_assets(order):
                if slot_key in stickers:
                    continue  # already in the set: never added twice
                with self.storage.open(asset.storage_key, "rb") as source:
                    content = source.read()
                uploaded = self.client.upload_sticker_file(user_id=user_id, content=sticker_png(content),
                                                           filename=f"sticker-{slot_key}.png")
                file_id = str((uploaded or {}).get("file_id") or "")
                if not file_id:
                    raise StickerSetError("uploadStickerFile returned no file_id")
                sticker = {"sticker": file_id, "format": "static", "emoji_list": [sticker_emoji(order, slot_key)]}
                if not exists:
                    self.client.create_new_sticker_set(user_id=user_id, name=name, title=record["title"], stickers=[sticker])
                    exists = True
                else:
                    self.client.add_sticker_to_set(user_id=user_id, name=name, sticker=sticker)
                stickers[slot_key] = file_id
                record.update({"stickers": stickers, "set_exists": True})
                self._save(run, record)
            record.update({"stickers": stickers, "set_exists": exists})
            if not record.get("link_message_id"):
                message = self.client.send_message(chat_id=user_id, text=LINK_TEXT.format(name=name))
                record["link_message_id"] = str((message or {}).get("message_id") or "")
            record.update({"status": "done", "error": "", "link": f"https://t.me/addstickers/{name}",
                           "finished_at": timezone.now().isoformat(), "actor_ref": actor_ref})
        except (TelegramAPIError, StickerSetError, OSError) as exc:
            error = getattr(exc, "description", "") or str(exc)
            logger.warning("telegram.sticker_set.failed order=%s error=%s", order.pk, error)
            record.update({"status": "failed", "error": str(error)[:500], "stickers": stickers,
                           "failed_at": timezone.now().isoformat(), "actor_ref": actor_ref})
            self._save(run, record)
            return record
        self._save(run, record)
        return record

    def _set_exists(self, name: str) -> bool:
        try:
            self.client.get_sticker_set(name=name)
        except TelegramAPIError as exc:
            if exc.status_code in (400, 404):
                return False  # STICKERSET_INVALID: not created yet
            raise
        return True

    @staticmethod
    def _final_assets(order: Order):
        from apps.core.services.final_delivery import FinalDeliveryService

        return FinalDeliveryService(adapter=None, storage=LocalMediaStorage()).delivery_set(order)

    @staticmethod
    @transaction.atomic
    def _save(run: FinalDelivery, record: dict) -> None:
        locked = FinalDelivery.objects.select_for_update().get(pk=run.pk)
        locked.summary = {**(locked.summary or {}), "sticker_set": dict(record)}
        locked.save(update_fields=["summary", "updated_at"])
        run.summary = locked.summary
