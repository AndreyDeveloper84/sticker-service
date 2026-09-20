"""Customer media lifecycle (DRF-2170).

What is deleted and when — and what never is:

- media: ``OrderPhoto`` source files, ``GeneratedAsset`` previews and finals
  (plus the provider original a QC normalization kept under
  ``metadata["normalized_from"]["storage_key"]``); the file leaves the
  disk, the row stays with ``metadata["purged"] = {at, rule, attempts,
  size_bytes, actor_ref}`` — «файл удалён <когда> <по правилу>» — and an
  OrderPhoto loses its ``original_filename``. A file that could NOT be
  deleted (file system error) is never marked purged: the row gets
  ``metadata["purge_failed"] = {attempts, last_error, last_attempt_at, rule,
  reason_ref}``, stays in every future plan and is retried by the next
  run; the operator is told «частично: осталось N файлов»;
- PII in the trail: the reason is a code from a fixed vocabulary
  (customer_request / legal / operator_other / retention); a free-text
  note is truncated to 120 characters, e-mails / phones / @usernames are
  masked, and it lives in the ``media.purged`` event only — never in the
  row metadata. ``actor_ref`` is the OPERATOR's username (never the
  customer's);
- contact: ``Order.selection["contact"]`` is cleared on a customer request
  (``contact_purged_at`` is written instead);
- never: Payment, GenerationJob (with its cost snapshot), OrderEvent,
  QcReport, FinalDelivery — the accounting trail.

Retention (env, all optional; the periods are the OWNER's decision):
``MEDIA_RETENTION_ENABLED`` (default false — nothing is ever deleted
automatically until the owner switches it on), and per kind
``MEDIA_RETENTION_SOURCE_PHOTOS_DAYS`` / ``MEDIA_RETENTION_PREVIEWS_DAYS`` /
``MEDIA_RETENTION_FINALS_DAYS`` counted from the moment the order became
terminal (DELIVERED / FAILED / CANCELLED); a kind without a period is kept.
Orders that are not terminal are never touched. ``manage.py
media_retention`` prints the plan (dry-run) and deletes only with
``--apply`` and the flag on.

A customer's deletion request: ``MediaLifecycleService.purge_order`` (and
the console action, superuser + reason) purges every media kind of a
terminal order right away, regardless of age, and clears the contact.

Every purge writes one ``OrderEvent media.purged`` per order with counts
and bytes — no file names, no contact, no PII.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.models import GeneratedAsset, Order, OrderEvent, OrderPhoto
from apps.core.services.budget import setting
from apps.core.storage import LocalMediaStorage

logger = logging.getLogger(__name__)

MEDIA_PURGED = OrderEvent.MEDIA_PURGED
PURGED_KEY = "purged"
PURGE_FAILED_KEY = "purge_failed"
TERMINAL_STATUSES = (Order.Status.DELIVERED, Order.Status.FAILED, Order.Status.CANCELLED)

KIND_SOURCE = "source_photos"
KIND_PREVIEW = "previews"
KIND_FINAL = "finals"
KINDS = (KIND_SOURCE, KIND_PREVIEW, KIND_FINAL)
RETENTION_SETTINGS = {
    KIND_SOURCE: "MEDIA_RETENTION_SOURCE_PHOTOS_DAYS",
    KIND_PREVIEW: "MEDIA_RETENTION_PREVIEWS_DAYS",
    KIND_FINAL: "MEDIA_RETENTION_FINALS_DAYS",
}
RULE_RETENTION = "retention"
RULE_CUSTOMER_REQUEST = "customer_request"

# Fixed vocabulary of reasons (what is stored); the free text is a note.
REASON_CUSTOMER_REQUEST = "customer_request"
REASON_LEGAL = "legal"
REASON_OPERATOR_OTHER = "operator_other"
REASON_RETENTION = "retention"
REASON_CODES = {
    REASON_CUSTOMER_REQUEST: "запрос клиента",
    REASON_LEGAL: "юридическое требование",
    REASON_OPERATOR_OTHER: "другое (оператор)",
}
NOTE_MAX_LENGTH = 120
_PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail
    re.compile(r"(?<![\w@])@[\w]{3,}"),  # @username
    re.compile(r"\+?\d[\d\s().-]{6,}\d"),  # phone-like digit runs
)
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"


class MediaLifecycleError(ValueError):
    pass


def mask_note(note: str) -> str:
    """Free text for the event only: ≤ 120 characters, e-mails / phones /
    @usernames replaced by «***»."""
    text = " ".join(str(note or "").split())[:NOTE_MAX_LENGTH]
    for pattern in _PII_PATTERNS:
        text = pattern.sub("***", text)
    return text


def error_text(exc: Exception) -> str:
    """«<ExceptionClass>: <message>» with any path-like token dropped, ≤ 200."""
    message = str(getattr(exc, "strerror", "") or exc)
    message = re.sub(r"['\"]?(?:[A-Za-z]:)?[\\/][^\s'\"]*['\"]?", "<path>", message)
    return f"{type(exc).__name__}: {message}"[:200]


# ------------------------------------------------------------- config


def retention_enabled() -> bool:
    raw = str(setting("MEDIA_RETENTION_ENABLED", "false")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def retention_days(kind: str) -> int | None:
    """Days for ``kind`` or None (= keep forever). A non-integer or negative
    value is treated as "not set" with a warning — never a default period."""
    name = RETENTION_SETTINGS[kind]
    raw = setting(name)
    if raw is None:
        return None
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("media_lifecycle: invalid %s=%r — kind %s is kept", name, raw, kind)
        return None
    if days < 0:
        logger.warning("media_lifecycle: negative %s=%r — kind %s is kept", name, raw, kind)
        return None
    return days


def retention_policy() -> dict:
    return {"enabled": retention_enabled(), "days": {kind: retention_days(kind) for kind in KINDS}}


# ------------------------------------------------------------- inventory


def closed_at(order: Order):
    """When the order became terminal: the last status change into its
    current terminal status, else ``updated_at``."""
    moments = [
        event.created_at
        for event in order.events.all()
        if event.event_type == OrderEvent.Type.STATUS_CHANGED and event.to_status == order.status
    ]
    return max(moments) if moments else order.updated_at


def is_purged(obj) -> bool:
    return bool((obj.metadata or {}).get(PURGED_KEY))


def purge_failed(obj) -> dict | None:
    return (obj.metadata or {}).get(PURGE_FAILED_KEY) or None


@dataclass
class PurgeItem:
    order: Order
    kind: str
    obj: object  # OrderPhoto | GeneratedAsset
    storage_keys: list[str]
    size_bytes: int


@dataclass
class PurgePlan:
    now: object
    policy: dict
    items: list[PurgeItem] = field(default_factory=list)

    @property
    def orders(self) -> list[Order]:
        seen = {}
        for item in self.items:
            seen.setdefault(item.order.pk, item.order)
        return list(seen.values())

    def counts(self) -> dict:
        counts = {kind: 0 for kind in KINDS}
        for item in self.items:
            counts[item.kind] += 1
        return counts

    @property
    def bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)


def media_items(order: Order, kinds=KINDS) -> list[PurgeItem]:
    """Every not-yet-purged media object of the order, by kind."""
    items = []
    if KIND_SOURCE in kinds:
        for photo in order.photos.all():
            if not is_purged(photo):
                items.append(PurgeItem(order, KIND_SOURCE, photo, [photo.storage_key], photo.size_bytes))
    for asset in order.generated_assets.all():
        kind = KIND_PREVIEW if asset.kind == GeneratedAsset.Kind.PREVIEW else KIND_FINAL
        if kind not in kinds or is_purged(asset):
            continue
        keys = [asset.storage_key]
        original = ((asset.metadata or {}).get("normalized_from") or {}).get("storage_key")
        if original and original != asset.storage_key:
            keys.append(str(original))
        items.append(PurgeItem(order, kind, asset, keys, asset.size_bytes))
    return items


# ------------------------------------------------------------- service


class MediaLifecycleService:
    def __init__(self, *, storage=None, now=None):
        self.storage = storage or LocalMediaStorage()
        self.now = now or timezone.now()

    # -- retention (scheduled) --

    def retention_plan(self) -> PurgePlan:
        """Terminal orders whose media of a kind with a period is older than
        that period. Nothing else is ever listed."""
        policy = retention_policy()
        plan = PurgePlan(now=self.now, policy=policy)
        active_kinds = [kind for kind in KINDS if policy["days"][kind] is not None]
        if not active_kinds:
            return plan
        orders = (
            Order.objects.filter(status__in=TERMINAL_STATUSES)
            .prefetch_related("events", "photos", "generated_assets")
            .order_by("pk")
        )
        for order in orders:
            closed = closed_at(order)
            due = [kind for kind in active_kinds if closed + timedelta(days=policy["days"][kind]) <= self.now]
            if due:
                plan.items.extend(media_items(order, due))
        return plan

    def apply_retention(self, plan: PurgePlan, *, actor_ref: str = "media_retention") -> dict:
        if not retention_enabled():
            raise MediaLifecycleError("MEDIA_RETENTION_ENABLED is not true — nothing was deleted")
        report = {"orders": 0, "files": 0, "bytes": 0, "errors": 0, "remaining": 0,
                  "counts": {kind: 0 for kind in KINDS}}
        for order in plan.orders:
            items = [item for item in plan.items if item.order.pk == order.pk]
            result = self._purge(order, items, rule=RULE_RETENTION, reason=REASON_RETENTION, note="",
                                 actor_ref=actor_ref, clear_contact=False)
            report["orders"] += 1
            for key in ("files", "bytes", "errors", "remaining"):
                report[key] += result[key]
            for kind in KINDS:
                report["counts"][kind] += result["counts"][kind]
        report["status"] = STATUS_COMPLETE if not report["remaining"] else STATUS_PARTIAL
        return report

    # -- customer request --

    def purge_order(self, order: Order, *, reason: str, actor_ref: str, note: str = "") -> dict:
        """Delete every media of a terminal order now and clear the contact;
        the accounting trail stays. ``reason`` is a REASON_CODES key; ``note``
        is free text kept (masked, truncated) in the event only. Objects
        already purged are skipped; a file that failed before is retried —
        every run tells the truth: complete or «partial: N remaining»."""
        reason = str(reason or "").strip()
        if reason not in REASON_CODES:
            raise MediaLifecycleError(f"A reason code is required: {', '.join(REASON_CODES)}")
        if order.status not in TERMINAL_STATUSES:
            raise MediaLifecycleError(
                f"Order #{order.pk} is not terminal ({order.status}) — media of an open order is never deleted"
            )
        return self._purge(order, media_items(order), rule=RULE_CUSTOMER_REQUEST, reason=reason, note=note,
                           actor_ref=actor_ref, clear_contact=True)

    # -- internals --

    @transaction.atomic
    def _purge(self, order: Order, items: list[PurgeItem], *, rule: str, reason: str, note: str, actor_ref: str,
               clear_contact: bool) -> dict:
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status not in TERMINAL_STATUSES:
            raise MediaLifecycleError(f"Order #{order.pk} is not terminal any more")
        moment = timezone.now()
        counts = {kind: 0 for kind in KINDS}
        files = 0
        errors = 0
        remaining = 0
        total = 0
        for item in items:
            obj = type(item.obj).objects.select_for_update().get(pk=item.obj.pk)
            if is_purged(obj):
                continue
            metadata = dict(obj.metadata or {})
            previous = metadata.get(PURGE_FAILED_KEY) or {}
            attempts = int(previous.get("attempts") or 0) + 1
            failure = ""
            for key in item.storage_keys:
                deleted, error = self._delete_file(key)
                if error:
                    errors += 1
                    failure = failure or error
                elif deleted:
                    files += 1
            if failure:
                # the file is still on disk: NOT purged — the object stays in
                # every future plan and the next run retries it
                remaining += 1
                metadata[PURGE_FAILED_KEY] = {"attempts": attempts, "last_error": failure,
                                              "last_attempt_at": moment.isoformat(), "rule": rule,
                                              "reason_ref": reason}
                obj.metadata = metadata
                obj.save(update_fields=["metadata", "updated_at"])
                continue
            metadata.pop(PURGE_FAILED_KEY, None)
            metadata[PURGED_KEY] = {"at": moment.isoformat(), "rule": rule, "reason_ref": reason,
                                    "attempts": attempts, "size_bytes": item.size_bytes,
                                    "actor_ref": str(actor_ref or "")}
            obj.metadata = metadata
            update_fields = ["metadata", "updated_at"]
            if isinstance(obj, OrderPhoto) and obj.original_filename:
                obj.original_filename = ""  # the customer's file name is not accounting
                update_fields.append("original_filename")
            obj.save(update_fields=update_fields)
            counts[item.kind] += 1
            total += item.size_bytes
        contact_cleared = False
        if clear_contact:
            selection = dict(locked.selection or {})
            if selection.get("contact"):
                selection["contact"] = ""
                selection["contact_purged_at"] = moment.isoformat()
                locked.selection = selection
                locked.save(update_fields=["selection", "updated_at"])
                contact_cleared = True
        status = STATUS_COMPLETE if not remaining else STATUS_PARTIAL
        if sum(counts.values()) or remaining or contact_cleared:
            OrderEvent.objects.create(
                order=locked,
                event_type=MEDIA_PURGED,
                actor_kind=OrderEvent.Actor.OPERATOR if rule == RULE_CUSTOMER_REQUEST else OrderEvent.Actor.SYSTEM,
                actor_ref=str(actor_ref or ""),
                payload={"rule": rule, "reason": reason, "reason_note": mask_note(note), "status": status,
                         "counts": counts, "files": files, "bytes": total, "errors": errors,
                         "remaining": remaining, "contact_cleared": contact_cleared},
            )
        logger.info("media_lifecycle.purged order=%s rule=%s status=%s files=%s errors=%s remaining=%s bytes=%s",
                    order.pk, rule, status, files, errors, remaining, total)
        return {"status": status, "counts": counts, "files": files, "bytes": total, "errors": errors,
                "remaining": remaining, "contact_cleared": contact_cleared}

    def _delete_file(self, key: str) -> tuple[bool, str]:
        """(deleted, error): (True, "") deleted; (False, "") was not there or
        an invalid key; (False, "<Class: message>") a file system error —
        logged, never raised, so one bad file cannot roll back the others,
        and never a purged mark for a file that is still on disk."""
        try:
            return self.storage.delete(key), ""
        except ValueError:
            logger.warning("media_lifecycle: invalid storage key skipped")
            return False, ""
        except OSError as exc:
            logger.warning("media_lifecycle: file delete failed (%s) — will be retried", type(exc).__name__)
            return False, error_text(exc)
