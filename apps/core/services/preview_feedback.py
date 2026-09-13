from django.db import transaction

from apps.core.models import GeneratedAsset, Order, Revision
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
        OrderStateService.transition(order=locked, to_status=Order.Status.REVISION_REQUESTED)
        return revision
