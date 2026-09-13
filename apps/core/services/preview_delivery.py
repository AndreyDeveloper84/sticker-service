from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from django.db import transaction
from django.utils import timezone

from apps.core.models import GeneratedAsset, Order
from apps.core.services.order_state import InvalidOrderTransition, OrderStateService
from apps.core.storage import LocalMediaStorage


class PreviewDeliveryError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    message_id: str
    metadata: dict


class PreviewDeliveryAdapter(Protocol):
    channel: str

    def send_preview(
        self,
        *,
        recipient_id: str,
        content: bytes,
        mime_type: str,
        filename: str,
        caption: str,
    ) -> DeliveryResult: ...


class PreviewDeliveryService:
    def __init__(self, *, adapter: PreviewDeliveryAdapter, storage=None):
        self.adapter = adapter
        self.storage = storage or LocalMediaStorage()

    def deliver(self, *, order: Order) -> GeneratedAsset:
        if order.channel_identity.channel != self.adapter.channel:
            raise PreviewDeliveryError("Delivery adapter does not match order channel")
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            raise PreviewDeliveryError(
                f"Order #{order.pk} cannot deliver preview from {order.status}"
            )

        approved = [
            asset
            for asset in order.generated_assets.filter(
                kind=GeneratedAsset.Kind.PREVIEW,
            ).select_related("job")
            if (asset.metadata or {}).get("internal_approved")
        ]
        if len(approved) != 1:
            raise PreviewDeliveryError(
                "Exactly one internally approved preview asset is required"
            )
        asset = approved[0]

        deliveries = list((asset.metadata or {}).get("deliveries") or [])
        successful = [
            item
            for item in deliveries
            if item.get("channel") == self.adapter.channel
            and item.get("status") == "sent"
        ]
        if successful:
            raise PreviewDeliveryError("Approved preview was already delivered")

        if not self.storage.exists(asset.storage_key):
            raise PreviewDeliveryError("Approved preview file is missing")
        with self.storage.open(asset.storage_key, "rb") as source:
            content = source.read()

        try:
            result = self.adapter.send_preview(
                recipient_id=order.channel_identity.external_user_id,
                content=content,
                mime_type=asset.mime_type or "image/png",
                filename=f"preview-{asset.pk}.png",
                caption="Ваш preview готов. Посмотрите результат и подтвердите или запросите правку.",
            )
        except Exception as exc:
            self._record_attempt(
                asset=asset,
                status="failed",
                message_id="",
                metadata={"error": str(exc)},
            )
            raise PreviewDeliveryError(str(exc)) from exc

        self._record_attempt(
            asset=asset,
            status="sent",
            message_id=str(result.message_id),
            metadata=result.metadata or {},
        )
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PREVIEW_REVIEW,
            )
        except InvalidOrderTransition as exc:
            raise PreviewDeliveryError(str(exc)) from exc
        return asset

    @transaction.atomic
    def _record_attempt(
        self,
        *,
        asset: GeneratedAsset,
        status: str,
        message_id: str,
        metadata: dict,
    ) -> None:
        locked = GeneratedAsset.objects.select_for_update().get(pk=asset.pk)
        value = dict(locked.metadata or {})
        deliveries = list(value.get("deliveries") or [])
        deliveries.append(
            {
                "channel": self.adapter.channel,
                "status": status,
                "message_id": message_id,
                "metadata": metadata,
                "created_at": timezone.now().isoformat(),
            }
        )
        value["deliveries"] = deliveries
        locked.metadata = value
        locked.save(update_fields=["metadata", "updated_at"])
