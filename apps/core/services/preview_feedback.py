from django.db import transaction

from apps.core.models import GeneratedAsset, Order, OrderEvent, Revision
from apps.core.services.order_state import OrderStateService


class PreviewFeedbackError(ValueError):
    pass


class PreviewFeedbackService:
    @staticmethod
    def _delivered_preview(order):
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by("-created_at")
        for asset in assets:
            deliveries = list((asset.metadata or {}).get("deliveries") or [])
            if (asset.metadata or {}).get("internal_approved") and any(
                item.get("status") == "sent" for item in deliveries
            ):
                return asset
        raise PreviewFeedbackError("No delivered approved preview")

    @classmethod
    @transaction.atomic
    def approve(cls, *, order: Order):
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.PREVIEW_REVIEW:
            raise PreviewFeedbackError("Order is not awaiting preview feedback")
        asset = cls._delivered_preview(locked)
        metadata = dict(asset.metadata or {})
        if not metadata.get("customer_approved"):
            metadata["customer_approved"] = True
            asset.metadata = metadata
            asset.save(update_fields=["metadata", "updated_at"])
            # DRF-2055: approval does not change order status, so it is the
            # only preview-feedback fact not visible in the status log.
            OrderEvent.objects.create(
                order=locked,
                event_type=OrderEvent.Type.PREVIEW_CUSTOMER_APPROVED,
                actor_kind=OrderEvent.Actor.CUSTOMER,
                payload={"asset_id": asset.pk},
            )
        return asset

    @classmethod
    @transaction.atomic
    def request_revision(cls, *, order: Order, category: str, customer_text: str = ""):
        locked = Order.objects.select_for_update().get(pk=order.pk)
        existing = Revision.objects.filter(order=locked).first()
        if existing:
            if locked.status in {
                Order.Status.REVISION_REQUESTED,
                Order.Status.REVISION_GENERATING,
            }:
                return existing
            raise PreviewFeedbackError("Included preview revision has already been used")
        if locked.status != Order.Status.PREVIEW_REVIEW:
            raise PreviewFeedbackError("Order is not awaiting preview feedback")
        valid_categories = {value for value, _ in Revision.Category.choices}
        if category not in valid_categories:
            raise PreviewFeedbackError("Unknown revision category")
        source = cls._delivered_preview(locked)
        revision = Revision.objects.create(
            order=locked,
            source_preview=source,
            category=category,
            customer_text=(customer_text or "").strip(),
        )
        # The customer has rejected this preview: its internal approval is
        # spent. Left in place, it made the console offer «Отправить превью
        # клиенту» for the OLD preview after the revision was generated —
        # a dead end (the delivery service refuses a second send). The
        # rejected preview stays on record as ``revision.source_preview``.
        cls._clear_internal_approval(source)
        OrderStateService.transition(order=locked, to_status=Order.Status.REVISION_REQUESTED)
        return revision

    @staticmethod
    def _clear_internal_approval(asset: GeneratedAsset) -> None:
        metadata = dict(asset.metadata or {})
        if not any(key in metadata for key in ("internal_approved", "internal_approved_at")):
            return
        metadata.pop("internal_approved", None)
        metadata.pop("internal_approved_at", None)
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])

    # «Сменить одежду» is the only category where the bot invites free text
    # ("во что переодеть"). The text is optional and accepted once, only while
    # the revision is still REQUESTED (not yet generating) and still empty.
    TEXT_CATEGORIES = frozenset({Revision.Category.CLOTHES})

    @classmethod
    def pending_text_revision(cls, *, identity):
        """The identity's requested revision that is waiting for optional
        customer text, or None (free text is then handled by the order flow)."""
        return (
            Revision.objects.filter(
                order__channel_identity=identity,
                order__status=Order.Status.REVISION_REQUESTED,
                status=Revision.Status.REQUESTED,
                category__in=cls.TEXT_CATEGORIES,
                customer_text="",
            )
            .order_by("-created_at")
            .first()
        )

    @classmethod
    @transaction.atomic
    def attach_revision_text(cls, *, identity, text: str):
        """Store the customer's optional text on the pending clothes revision.
        Returns the revision, or None when nothing is waiting for text."""
        text = (text or "").strip()
        if not text:
            return None
        pending = cls.pending_text_revision(identity=identity)
        if pending is None:
            return None
        revision = Revision.objects.select_for_update().get(pk=pending.pk)
        if revision.status != Revision.Status.REQUESTED or revision.customer_text:
            return None
        revision.customer_text = text[:500]
        revision.save(update_fields=["customer_text", "updated_at"])
        return revision
