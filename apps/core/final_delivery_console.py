import os

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from apps.core.console_html import buttons_html, lines_html
from apps.core.models import Order
from apps.core.qc_console import QcOrderAdmin
from apps.core.services.final_delivery import (
    FinalDeliveryError,
    FinalDeliveryService,
)
from apps.core.services.qc import QcError
from apps.max_bot.client import MaxBotClient
from apps.max_bot.final_delivery import MaxFinalDeliveryAdapter
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.final_delivery import TelegramFinalDeliveryAdapter

# One synchronous console request sends at most this many stickers so a
# 9-item pack cannot outlive the worker timeout; the operator re-runs
# "Resume delivery" until the plan is complete.
CONSOLE_MAX_ITEMS = 3


class FinalDeliveryOrderAdmin(QcOrderAdmin):
    """Production Console final delivery surface (DRF-2053).

    Layered on the QC console (DRF-2052): the QC panel/actions and the
    production/preview surfaces below it are inherited unchanged; this
    class only adds the delivery panel and its two operator actions.
    """

    readonly_fields = QcOrderAdmin.readonly_fields + ("final_delivery_panel",)
    fields = QcOrderAdmin.fields + ("final_delivery_panel",)

    def get_final_delivery_service(self, order):
        if order.channel_identity.channel == "telegram":
            adapter = TelegramFinalDeliveryAdapter(
                client=TelegramBotClient(os.getenv("TELEGRAM_BOT_TOKEN", ""))
            )
        elif order.channel_identity.channel == "max":
            adapter = MaxFinalDeliveryAdapter(
                client=MaxBotClient(os.getenv("MAX_BOT_TOKEN", ""))
            )
        else:
            raise FinalDeliveryError("Unsupported delivery channel")
        return FinalDeliveryService(adapter=adapter)

    @admin.display(description="Final delivery — набор клиенту")
    def final_delivery_panel(self, order):
        if not order or not order.pk:
            return "—"
        if order.status not in {
            Order.Status.READY_FOR_DELIVERY,
            Order.Status.DELIVERY_IN_PROGRESS,
            Order.Status.DELIVERED,
        } and not order.final_deliveries.exists():
            return "Доставка доступна после QC PASS (READY_FOR_DELIVERY)."
        service = self.get_final_delivery_service(order)
        try:
            plan = service.delivery_plan(order)
        except FinalDeliveryError as exc:
            return format_html("<strong>Ошибка набора:</strong> {}", str(exc))

        gate_blocked = ""
        if order.status != Order.Status.DELIVERED:
            try:
                service.qc.assert_delivery_allowed(order=order)
            except QcError as exc:
                gate_blocked = str(exc)

        actions = []
        if gate_blocked:
            pass  # no send action is offered while the QC gate rejects the set
        elif order.status == Order.Status.READY_FOR_DELIVERY:
            actions.append(
                (reverse("admin:core_order_deliver_final", args=[order.pk]), "Deliver final set")
            )
        elif order.status == Order.Status.DELIVERY_IN_PROGRESS:
            actions.append(
                (
                    reverse("admin:core_order_resume_final_delivery", args=[order.pk]),
                    "Resume delivery",
                )
            )
        lines = [
            format_html(
                "<strong>Runs: {} · Summary message: {}</strong>",
                plan.attempts,
                plan.summary_status,
            )
        ]
        if gate_blocked:
            lines.append(format_html("<strong>QC gate:</strong> заблокирован ({})", gate_blocked))
        for slot in plan.slots:
            if slot.asset_id:
                asset_link = format_html(
                    ' · <a href="{}" target="_blank" rel="noopener">asset #{}</a>',
                    reverse("admin:core_preview_asset_file", args=[slot.asset_id]),
                    slot.asset_id,
                )
            else:
                asset_link = " · NO CURRENT ASSET (set incomplete)"
            detail = ""
            if slot.status == "sent":
                detail = f" · message={slot.message_id or '—'} · run {slot.attempt}"
            elif slot.status == "failed":
                detail = (
                    f" · {slot.failure_class or 'failed'}: {slot.error or 'unknown error'}"
                    f" · run {slot.attempt}"
                )
            lines.append(
                format_html("{} · {}{}{}", slot.slot_key, slot.status, asset_link, detail)
            )
        if actions:
            lines.append(
                buttons_html(actions)
            )
        return lines_html(lines)

    def get_urls(self):
        custom = [
            path(
                "<int:order_id>/deliver-final/",
                self.admin_site.admin_view(self.deliver_final_view),
                name="core_order_deliver_final",
            ),
            path(
                "<int:order_id>/resume-final-delivery/",
                self.admin_site.admin_view(self.resume_final_delivery_view),
                name="core_order_resume_final_delivery",
            ),
        ]
        return custom + super().get_urls()

    @staticmethod
    def _delivery_plan_message(plan) -> str:
        summary = ", ".join(f"{slot.slot_key}: {slot.status}" for slot in plan.slots)
        if plan.complete:
            return f"Final set delivered: {summary}"
        failed = [slot.slot_key for slot in plan.slots if slot.status == "failed"]
        hint = " — запустите Resume delivery, чтобы дослать оставшиеся стикеры."
        if failed:
            hint = (
                f" — сбой на slots {', '.join(failed)}; проверьте причину и "
                "запустите Resume delivery (уже отправленные не дублируются)."
            )
        return f"Delivery plan: {summary}; summary message: {plan.summary_status}{hint}"

    def deliver_final_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_deliver_final", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Deliver final set",
                action_url=action_url,
                detail=(
                    "Финальные стикеры будут отправлены клиенту в канал заказа в "
                    f"порядке выбора эмоций (до {CONSOLE_MAX_ITEMS} за запуск). "
                    "Заказ перейдёт в DELIVERY_IN_PROGRESS и станет DELIVERED, "
                    "когда отправлен весь набор и финальное сообщение."
                ),
            )
        if order.status != Order.Status.READY_FOR_DELIVERY:
            self.message_user(
                request,
                "Deliver final set доступен только из READY_FOR_DELIVERY.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            plan = self.get_final_delivery_service(order).deliver(
                order=order, max_items=CONSOLE_MAX_ITEMS
            )
        except FinalDeliveryError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._delivery_plan_message(plan), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def resume_final_delivery_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_resume_final_delivery", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Resume final delivery",
                action_url=action_url,
                detail=(
                    "Будут отправлены только ещё не доставленные стикеры "
                    f"(до {CONSOLE_MAX_ITEMS} за запуск); уже отправленные "
                    "клиенту не дублируются."
                ),
            )
        try:
            plan = self.get_final_delivery_service(order).resume(
                order=order, max_items=CONSOLE_MAX_ITEMS
            )
        except FinalDeliveryError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._delivery_plan_message(plan), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))


def install_final_delivery_console():
    """Replace the QC console registration with its delivery-aware subclass.

    Same layering as the production -> preview -> QC consoles: exactly one
    Order ModelAdmin stays registered, and it is a QcOrderAdmin.
    """
    try:
        admin.site.unregister(Order)
    except admin.sites.NotRegistered:
        pass
    admin.site.register(Order, FinalDeliveryOrderAdmin)
