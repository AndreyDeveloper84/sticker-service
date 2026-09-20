import logging
import os

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from apps.core.console_html import buttons_html, lines_html
from apps.core.console_text import (
    DELIVERY_FAILURE_CLASSES,
    DELIVERY_SLOT_STATES,
    humanize_error,
    label,
    slot_title,
)
from apps.core.models import Order
from apps.core.qc_console import QcOrderAdmin
from apps.core.services.final_delivery import (
    FinalDeliveryError,
    FinalDeliveryService,
)
from apps.core.services.qc import QcError
from apps.max_bot.client import MaxBotClient
from apps.max_bot.final_delivery import MaxFinalDeliveryAdapter
from apps.core.models import ChannelIdentity
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.final_delivery import TelegramFinalDeliveryAdapter
from apps.telegram_bot.sticker_set import StickerSetError, TelegramStickerSetService, sticker_set_record

logger = logging.getLogger(__name__)

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

    panel_sections = QcOrderAdmin.panel_sections + (("Доставка", "final_delivery_panel"),)

    def secondary_links(self, order):
        links = super().secondary_links(order)
        if order.status == Order.Status.DELIVERY_IN_PROGRESS and self._gate_open(order):
            links.append(
                (
                    "Продолжить доставку",
                    reverse("admin:core_order_resume_final_delivery", args=[order.pk]),
                    f"Досылает только ещё не отправленные стикеры (до {CONSOLE_MAX_ITEMS} за нажатие); "
                    "уже отправленные не дублируются.",
                )
            )
        return links

    def _gate_open(self, order):
        try:
            self.get_qc_service().assert_delivery_allowed(order=order)
        except QcError:
            return False
        return True

    # ------------------------------------------------ Telegram sticker set (DRF-2163)

    def get_sticker_set_service(self):
        """None when no bot token is configured (tests / non-Telegram
        deployments): the set is then not attempted and nothing is said —
        a real Telegram delivery already used the same token."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return None
        return TelegramStickerSetService(client=TelegramBotClient(token))

    def _after_delivery(self, request, order, plan):
        """A completed Telegram delivery → the customer's sticker set + the
        t.me/addstickers link (at-most-once, errors shown to the operator)."""
        if not plan.complete or order.channel_identity.channel != ChannelIdentity.Channel.TELEGRAM:
            return
        order.refresh_from_db()
        if order.status != Order.Status.DELIVERED:
            return
        self._ensure_sticker_set(request, order)

    def _ensure_sticker_set(self, request, order):
        service = self.get_sticker_set_service()
        if service is None:
            logger.info("telegram.sticker_set.skipped order=%s reason=no_bot_token", order.pk)
            return None
        try:
            record = service.ensure(order, actor_ref=request.user.get_username())
        except (StickerSetError, FinalDeliveryError) as exc:
            self.message_user(request, f"Набор стикеров Telegram не создан: {exc}", level=messages.WARNING)
            return None
        if record.get("status") == "done":
            self.message_user(
                request,
                f"Набор стикеров Telegram готов: {record['link']} ({len(record.get('stickers') or {})} стикеров), "
                "ссылка отправлена клиенту.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f"Набор стикеров Telegram не создан: {record.get('error') or 'ошибка Bot API'} — "
                "нажмите «Создать набор повторно» в блоке «Доставка».",
                level=messages.WARNING,
            )
        return record

    def sticker_set_line(self, order):
        """Panel line: the set link / the failure + the retry button, Telegram only."""
        if order.channel_identity.channel != ChannelIdentity.Channel.TELEGRAM:
            return None
        record = sticker_set_record(order)
        retry = reverse("admin:core_order_create_sticker_set", args=[order.pk])
        if record is None:
            if order.status != Order.Status.DELIVERED:
                return None
            return format_html(
                "Набор стикеров Telegram: ещё не создан · {}",
                buttons_html([(retry, "Создать набор повторно")]),
            )
        count = len(record.get("stickers") or {})
        if record.get("status") == "done":
            return format_html(
                'Набор стикеров Telegram: <a href="{}" target="_blank" rel="noopener">{}</a> · {} стикеров · '
                "ссылка клиенту: {}",
                record["link"], record["set_name"], count, "отправлена" if record.get("link_message_id") else "—",
            )
        return format_html(
            "Набор стикеров Telegram: <strong>сбой</strong> — {} · добавлено {} стикеров · {}",
            record.get("error") or "ошибка Bot API", count, buttons_html([(retry, "Создать набор повторно")]),
        )

    def create_sticker_set_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        change_url = reverse("admin:core_order_change", args=[order.pk])
        action_url = reverse("admin:core_order_create_sticker_set", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Создать набор повторно",
                action_url=action_url,
                detail=(
                    "Стикеры заказа будут добавлены в набор клиента в Telegram (только ещё не добавленные), "
                    "клиенту отправится ссылка t.me/addstickers/… (если ещё не отправлялась). "
                    "Доставленные файлы не пересылаются."
                ),
            )
        if order.status != Order.Status.DELIVERED or order.channel_identity.channel != ChannelIdentity.Channel.TELEGRAM:
            self.message_user(request, "Набор создаётся после полной доставки заказа в Telegram.", level=messages.ERROR)
            return redirect(change_url)
        self._ensure_sticker_set(request, order)
        return redirect(change_url)

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

    @admin.display(description="Доставка набора клиенту")
    def final_delivery_panel(self, order):
        if not order or not order.pk:
            return "—"
        if order.status not in {
            Order.Status.READY_FOR_DELIVERY,
            Order.Status.DELIVERY_IN_PROGRESS,
            Order.Status.DELIVERED,
        } and not order.final_deliveries.exists():
            return "Доставка станет доступна после прохождения контроля качества."
        service = self.get_final_delivery_service(order)
        try:
            plan = service.delivery_plan(order)
        except FinalDeliveryError as exc:
            return format_html("<strong>Ошибка набора:</strong> {}", humanize_error(exc))

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
                (reverse("admin:core_order_deliver_final", args=[order.pk]), "Отправить набор клиенту")
            )
        elif order.status == Order.Status.DELIVERY_IN_PROGRESS:
            actions.append(
                (
                    reverse("admin:core_order_resume_final_delivery", args=[order.pk]),
                    "Продолжить доставку",
                )
            )
        lines = [
            format_html(
                "<strong>Запусков доставки: {} · Финальное сообщение: {}</strong>",
                plan.attempts,
                label(DELIVERY_SLOT_STATES, plan.summary_status),
            )
        ]
        if gate_blocked:
            lines.append(
                format_html(
                    "<strong>Контроль качества:</strong> доставка заблокирована ({})",
                    humanize_error(QcError(gate_blocked)),
                )
            )
        for slot in plan.slots:
            if slot.asset_id:
                asset_link = format_html(
                    ' · файл #{} · <a href="{}" target="_blank" rel="noopener">Открыть</a>',
                    slot.asset_id,
                    reverse("admin:core_preview_asset_file", args=[slot.asset_id]),
                )
            else:
                asset_link = " · НЕТ ГОТОВОГО ФАЙЛА (набор неполный)"
            detail = ""
            if slot.status == "sent":
                detail = f" · сообщение {slot.message_id or '—'} · запуск {slot.attempt}"
            elif slot.status == "failed":
                detail = (
                    f" · {label(DELIVERY_FAILURE_CLASSES, slot.failure_class, 'сбой')}: "
                    f"{slot.error or 'неизвестная ошибка'} · запуск {slot.attempt}"
                )
            lines.append(
                format_html(
                    "Слот {} · {}{}{}",
                    slot_title(order, slot.slot_key),
                    label(DELIVERY_SLOT_STATES, slot.status),
                    asset_link,
                    detail,
                )
            )
        sticker_line = self.sticker_set_line(order)
        if sticker_line:
            lines.append(sticker_line)
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
                "<int:order_id>/create-sticker-set/",
                self.admin_site.admin_view(self.create_sticker_set_view),
                name="core_order_create_sticker_set",
            ),
            path(
                "<int:order_id>/resume-final-delivery/",
                self.admin_site.admin_view(self.resume_final_delivery_view),
                name="core_order_resume_final_delivery",
            ),
        ]
        return custom + super().get_urls()

    @staticmethod
    def _delivery_plan_message(plan, order=None) -> str:
        def name(key):
            return slot_title(order, key) if order is not None else key

        summary = ", ".join(
            f"{name(slot.slot_key)}: {label(DELIVERY_SLOT_STATES, slot.status)}" for slot in plan.slots
        )
        if plan.complete:
            return f"Набор доставлен клиенту: {summary}"
        failed = [name(slot.slot_key) for slot in plan.slots if slot.status == "failed"]
        hint = " — нажмите «Продолжить доставку», чтобы дослать оставшиеся стикеры."
        if failed:
            hint = (
                f" — сбой на слотах {', '.join(failed)}; проверьте причину и "
                "нажмите «Продолжить доставку» (уже отправленные не дублируются)."
            )
        return (
            f"Доставка: {summary}; финальное сообщение: "
            f"{label(DELIVERY_SLOT_STATES, plan.summary_status)}{hint}"
        )

    def deliver_final_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_deliver_final", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Отправить набор клиенту",
                action_url=action_url,
                detail=(
                    "Готовые стикеры будут отправлены клиенту в канал заказа в "
                    f"порядке выбора эмоций (до {CONSOLE_MAX_ITEMS} за нажатие). "
                    "Заказ перейдёт в «Доставка» и станет «Доставлен», "
                    "когда отправлен весь набор и финальное сообщение."
                ),
            )
        if order.status != Order.Status.READY_FOR_DELIVERY:
            self.message_user(
                request,
                "Отправка набора доступна только из статуса «Готов к доставке».",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            plan = self.get_final_delivery_service(order).deliver(
                order=order, max_items=CONSOLE_MAX_ITEMS
            )
        except FinalDeliveryError as exc:
            self._fail(request, exc)
        else:
            self.message_user(request, self._delivery_plan_message(plan, order), level=messages.SUCCESS)
            self._after_delivery(request, order, plan)
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
                title="Продолжить доставку",
                action_url=action_url,
                detail=(
                    "Будут отправлены только ещё не доставленные стикеры "
                    f"(до {CONSOLE_MAX_ITEMS} за нажатие); уже отправленные "
                    "клиенту не дублируются."
                ),
            )
        try:
            plan = self.get_final_delivery_service(order).resume(
                order=order, max_items=CONSOLE_MAX_ITEMS
            )
        except FinalDeliveryError as exc:
            self._fail(request, exc)
        else:
            self.message_user(request, self._delivery_plan_message(plan, order), level=messages.SUCCESS)
            self._after_delivery(request, order, plan)
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
