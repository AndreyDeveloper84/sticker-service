"""«Требует внимания» page on the Production Console (DRF-2167).

One read-only view over ``AttentionQueue``: every live order in exactly one
bucket, with the age, the fact behind it and the next step as a link to the
order card. The service does the classification; this module only renders.
"""

from __future__ import annotations

from django.template.response import TemplateResponse
from django.urls import reverse

from apps.core.services.attention import BUCKETS, AttentionQueue, age_text


def build_context(buckets: dict) -> dict:
    sections = []
    total = 0
    for key, title in BUCKETS:
        rows = [
            {
                "order_id": item.order.pk,
                "url": reverse("admin:core_order_change", args=[item.order.pk]),
                "channel": item.order.channel_identity.get_channel_display(),
                "product": item.order.product.name,
                "status": item.order.get_status_display(),
                "age": age_text(item.age),
                "detail": item.detail,
                "next_step": item.next_step,
            }
            for item in buckets.get(key, [])
        ]
        total += len(rows)
        sections.append({"key": key, "title": title, "rows": rows})
    return {"sections": sections, "total": total}


class AttentionViews:
    """Mixed into ProductionOrderAdmin; wired in get_urls."""

    def attention_view(self, request):
        buckets = AttentionQueue().build()
        context = {
            **self.admin_site.each_context(request),
            "title": "Требует внимания",
            **build_context(buckets),
        }
        return TemplateResponse(request, "admin/core/order/attention.html", context)
