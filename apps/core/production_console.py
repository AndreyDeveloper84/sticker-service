import os
from io import BytesIO

from django.contrib import admin, messages
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from PIL import Image

from .attention_console import AttentionViews
from .console_generation import (
    active_jobs,
    dequeue_candidates,
    last_result_text,
    latest_finished,
    queue_wait_text,
    waiting_headline,
    worker_health,
)
from .console_html import LINE_BREAK, lines_html
from .pilot_analytics_console import PilotAnalyticsViews
from .console_text import (
    generation_wait,
    JOB_STATUSES,
    JOB_TASKS,
    PAID_CALL_ONE,
    PAID_CALL_PER_SLOT,
    PRODUCTION_SLOT_STATES,
    REVISION_CATEGORIES,
    REVISION_STATUSES,
    chat_contact,
    custom_phrases,
    customer_contact,
    customer_contact_skipped,
    humanize_error,
    job_error_text,
    label,
    slot_title,
)
from .image_providers import get_image_provider
from .models import ChannelIdentity, GeneratedAsset, GenerationJob, Order, OrderPhoto, Payment, Product, Revision
from .services.budget import BudgetConfigError, BudgetError, BudgetExceeded, BudgetOverride, BudgetService
from .services.generation_cost import format_ai_total, format_known_cost, format_tokens
from .services.order_economics import (
    NOT_COMPUTABLE,
    PROVIDER_CONFIRMED,
    RATE_SOURCE_CONFIG_SNAPSHOT,
    RATE_SOURCE_NOT_CONFIGURED,
    STAGE_FULL,
    STAGE_PREVIEW,
    STAGE_REGENERATION,
    STAGE_REVISION,
    STAGE_TITLES,
    OrderEconomics,
    money,
    unknown_component_text,
)
from .services.full_production import FullProductionError, FullProductionService
from .services import generation_queue
from .services.generation import GenerationError, GenerationService, reap_stale
from .services.order_close import PAYMENT_NOTES, OrderCloseError, OrderCloseService
from .services.order_state import InvalidOrderTransition, OrderStateService
from .services.qc import QcError, QcService
from .storage import LocalMediaStorage
from apps.max_bot.client import MaxBotClient
from apps.max_bot.production_notice import notify_customer_production_started
from apps.telegram_bot.client import TelegramBotClient
from apps.telegram_bot.payments import TelegramPaymentError, TelegramStarsPaymentAdapter

# Status colour of the list badge: the operator scans for "needs me now".
STATUS_COLORS = {
    Order.Status.PAID: "#1d6f42",
    Order.Status.INTERNAL_PREVIEW_REVIEW: "#b26a00",
    Order.Status.REVISION_REQUESTED: "#b26a00",
    Order.Status.PACK_GENERATING: "#1c5d99",
    Order.Status.QUALITY_CONTROL: "#b26a00",
    Order.Status.READY_FOR_DELIVERY: "#1d6f42",
    Order.Status.DELIVERY_IN_PROGRESS: "#1c5d99",
    Order.Status.DELIVERED: "#5a5a5a",
    Order.Status.CANCELLED: "#8a8a8a",
    Order.Status.FAILED: "#a12622",
}


class RussianStatusFilter(admin.SimpleListFilter):
    title = "статус"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return list(Order.Status.choices)

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class RussianProductFilter(admin.SimpleListFilter):
    title = "продукт"
    parameter_name = "product"

    def lookups(self, request, model_admin):
        return [(p.pk, p.name) for p in Product.objects.order_by("-is_active", "id")]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(product_id=self.value())
        return queryset


class RussianChannelFilter(admin.SimpleListFilter):
    title = "канал"
    parameter_name = "channel"

    def lookups(self, request, model_admin):
        return list(ChannelIdentity.Channel.choices)

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(channel_identity__channel=self.value())
        return queryset


def asset_dimensions(storage, asset):
    """«493×512» from the image header, or '' when the file is unreadable."""
    try:
        if not storage.exists(asset.storage_key):
            return ""
        with storage.open(asset.storage_key, "rb") as source:
            with Image.open(BytesIO(source.read())) as image:
                return f"{image.width}×{image.height}"
    except Exception:
        return ""


class ProductionOrderPhotoInline(admin.TabularInline):
    model = OrderPhoto
    extra = 0
    can_delete = False
    verbose_name = "фото клиента"
    verbose_name_plural = "Фото клиента"
    fields = (
        "open_photo",
        "original_filename",
        "mime_type",
        "size_bytes",
        "status",
        "created_at",
    )
    readonly_fields = fields

    @admin.display(description="Фото")
    def open_photo(self, photo):
        if not photo.pk:
            return "—"
        url = reverse("admin:core_orderphoto_file", args=[photo.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener">Открыть фото</a>', url)

    def has_add_permission(self, request, obj=None):
        return False


class ProductionOrderAdmin(AttentionViews, PilotAnalyticsViews, admin.ModelAdmin):
    """Production Console (DRF-2084: Russian, «Следующий шаг», secondary actions).

    Subclasses (preview delivery → QC → final delivery) add their panels
    through ``panel_sections`` and their secondary actions through
    ``secondary_links``; the page layout itself is composed here once.
    """

    list_display = (
        "order_number",
        "channel",
        "customer",
        "product_title",
        "style_title",
        "status_badge",
        "photo_count",
        "calls_cost",
        "created_at",
    )
    list_filter = (RussianStatusFilter, RussianChannelFilter, RussianProductFilter, "style")
    change_list_template = "admin/core/order/change_list.html"
    change_form_template = "admin/core/order/change_form.html"
    search_fields = (
        "=id",
        "channel_identity__external_user_id",
        "channel_identity__username",
        "channel_identity__display_name",
    )
    list_select_related = ("user", "channel_identity", "product", "style")
    inlines = (ProductionOrderPhotoInline,)
    ordering = ("-created_at",)

    # (section title, readonly field name) panels appended by the console
    # layers, in page order.
    panel_sections = ()

    # ------------------------------------------------------------ layout

    def get_readonly_fields(self, request, obj=None):
        return (
            "next_step",
            "user",
            "channel_identity",
            "product",
            "style",
            "status_title",
            "customer_block",
            "payment_block",
            "custom_phrases_block",
            "customer_notes",
            "revision_request",
            "preview_assets",
            "production_plan",
            "generation_history",
            "expenses",
            "economics",
            *[name for _title, name in self.panel_sections],
            "secondary_actions",
            "created_at",
            "updated_at",
        )

    def get_fieldsets(self, request, obj=None):
        sections = [
            ("Следующий шаг", {"fields": ("next_step",)}),
            (
                "Заказ",
                {
                    "fields": (
                        "status_title",
                        "customer_block",
                        "payment_block",
                        "product",
                        "style",
                        "custom_phrases_block",
                        "customer_notes",
                        "operator_notes",
                    )
                },
            ),
            ("Превью", {"fields": ("preview_assets", "revision_request")}),
            ("Производство", {"fields": ("production_plan", "generation_history")}),
            ("Расходы", {"fields": ("expenses",)}),
            ("Экономика заказа", {"fields": ("economics",)}),
        ]
        for title, name in self.panel_sections:
            sections.append((title, {"fields": (name,)}))
        sections.append(
            ("Дополнительно", {"classes": ("collapse",), "fields": ("secondary_actions",)})
        )
        sections.append(
            ("Служебное", {"classes": ("collapse",), "fields": ("user", "created_at", "updated_at")})
        )
        return sections

    # ------------------------------------------------------------- list

    @admin.display(description="№", ordering="id")
    def order_number(self, order):
        return f"#{order.pk}"

    @admin.display(description="Канал", ordering="channel_identity__channel")
    def channel(self, order):
        return order.channel_identity.get_channel_display()

    @admin.display(description="Клиент", ordering="channel_identity__external_user_id")
    def customer(self, order):
        identity = order.channel_identity
        name = identity.display_name or identity.username or identity.external_user_id
        return f"{name} ({identity.external_user_id})"

    @admin.display(description="Продукт", ordering="product__name")
    def product_title(self, order):
        return order.product.name

    @admin.display(description="Стиль", ordering="style__name")
    def style_title(self, order):
        return order.style.name

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, order):
        color = STATUS_COLORS.get(order.status, "#1c5d99")
        return format_html(
            '<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            'color:#fff;background:{};white-space:nowrap">{}</span>',
            color,
            order.get_status_display(),
        )

    @admin.display(description="Фото")
    def photo_count(self, order):
        return order.photos.count()

    @admin.display(description="Статус")
    def status_title(self, order):
        if not order or not order.pk:
            return "—"
        return self.status_badge(order)

    @admin.display(description="Клиент")
    def customer_block(self, order):
        if not order or not order.pk:
            return "—"
        identity = order.channel_identity
        lines = [
            format_html(
                "{} · {} · id {}{}",
                identity.get_channel_display(),
                identity.display_name or "—",
                identity.external_user_id,
                f" · @{identity.username}" if identity.username else "",
            )
        ]
        contact = customer_contact(order)
        if contact:
            lines.append(format_html("Контакт для связи: <strong>{}</strong>", contact))
        elif customer_contact_skipped(order):
            # the customer chose «свяжемся здесь»: the chat is the contact
            lines.append(format_html("Контакт для связи: в этом чате — <strong>{}</strong>", chat_contact(identity)))
        elif (order.product.config or {}).get("requires_customer_contact"):
            lines.append("Контакт для связи: ещё не указан")
        return lines_html(lines)

    @admin.display(description="Оплата")
    def payment_block(self, order):
        """The customer's payment and, for a confirmed Telegram Stars payment,
        the operator refund action (owner GO 2026-09-20). A refunded payment
        is shown as such; the order status is the operator's separate call."""
        if not order or not order.pk:
            return "—"
        payments = sorted(order.payments.all(), key=lambda p: (p.confirmed_at or p.created_at, p.pk))
        made = [p for p in payments if p.status in (Payment.Status.CONFIRMED, Payment.Status.REFUNDED)]
        if not made:
            pending = [p for p in payments if p.status == Payment.Status.PENDING]
            if not pending:
                return "Платежей нет."
            return "Ожидается: " + ", ".join(f"{money(p.amount_minor, p.currency)} ({p.provider})" for p in pending)
        payment = made[-1]
        if payment.status == Payment.Status.REFUNDED:
            refund = (payment.metadata or {}).get("refund") or {}
            moment = str(refund.get("refunded_at") or "")[:16].replace("T", " ")
            return lines_html([
                format_html(
                    "<strong>Оплата возвращена</strong>: {} ({}) · {} · {} · причина: {}",
                    money(payment.amount_minor, payment.currency), payment.provider, moment or "—",
                    refund.get("actor_ref") or "—", refund.get("reason") or "—",
                ),
                "Статус заказа не менялся автоматически — решение по заказу за оператором.",
            ])
        lines = [f"Оплачено: {money(payment.amount_minor, payment.currency)} ({payment.provider})"]
        if payment.provider == TelegramStarsPaymentAdapter.provider:
            lines.append(format_html(
                '<a class="button" href="{}">Вернуть звёзды</a> — возврат {} клиенту '
                "(только суперпользователь, с указанием причины)",
                reverse("admin:core_order_refund_stars", args=[order.pk]),
                money(payment.amount_minor, payment.currency),
            ))
        return lines_html(lines)

    @admin.display(description="Фразы для стикеров")
    def custom_phrases_block(self, order):
        if not order or not order.pk:
            return "—"
        phrases = custom_phrases(order)
        if not phrases:
            if (order.product.config or {}).get("requires_custom_phrases"):
                return "Клиент ещё не прислал фразы."
            return "—"
        return format_html(
            "<ol style=\"margin:0;padding-left:1.4em\">{}</ol>",
            format_html_join("", "<li>{}</li>", ((phrase,) for phrase in phrases)),
        )

    # -------------------------------------------------------- next step

    def _latest_preview(self, order):
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).select_related("job")
        for asset in assets.order_by("-created_at", "-pk"):
            if asset.job.status == GenerationJob.Status.SUCCEEDED:
                return asset
        return None

    @staticmethod
    def _preview_sent(asset) -> bool:
        return any(
            item.get("status") == "sent" for item in ((asset.metadata or {}).get("deliveries") or [])
        )

    def _approved_preview_to_send(self, order):
        """The internally approved preview that can still go to the customer.

        A preview that was already sent is never a candidate: the delivery
        service refuses a second send, so offering it as «Отправить превью
        клиенту» is a dead end (seen live on the first revision: the old
        preview kept its approval flag before request_revision cleared it)."""
        for asset in order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW):
            if (asset.metadata or {}).get("internal_approved") and not self._preview_sent(asset):
                return asset
        return None

    def _production_plan_safe(self, order):
        try:
            return self.get_full_production_service().production_plan(order)
        except FullProductionError:
            return []

    # ------------------------------------------------- background jobs

    def active_jobs(self, order):
        """Queued / running attempts the card must wait for (D-1)."""
        if not order or not order.pk:
            return []
        return active_jobs(order)

    def _refuse_if_generating(self, request, order) -> bool:
        """Server-side twin of the hidden buttons: while an attempt is queued
        or running, every action that would create a job, move the order or
        send anything to the customer is refused with one Russian message.
        The services guard too (ALREADY_RUNNING / QUEUED); this keeps the
        wording uniform and costs no service call."""
        jobs = self.active_jobs(order)
        if not jobs:
            return False
        headline, _explanation = waiting_headline(order, jobs)
        self.message_user(
            request,
            f"{headline} — дождитесь результата, действия пока недоступны.",
            level=messages.ERROR,
        )
        return True

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        generating = False
        if object_id:
            order = self.get_object(request, object_id)
            if order is not None:
                # A RUNNING job older than the stale limit is failed closed
                # right here (worker gone), so the operator sees «неоднозначно»
                # now, not after the next paid click.
                if any(job.status == GenerationJob.Status.RUNNING for job in order.generation_jobs.all()):
                    if reap_stale(order):
                        order = self.get_object(request, object_id)
                generating = bool(self.active_jobs(order))
        extra_context = {**(extra_context or {}), "generation_active": generating}
        return super().changeform_view(request, object_id, form_url, extra_context=extra_context)

    def next_step_for(self, order):
        """(headline, explanation, button label, url, warning) for the status.

        Exactly one main button per status; everything else lives in the
        «Дополнительно» block (secondary_links)."""
        jobs = self.active_jobs(order)
        if jobs:
            # queued / running attempt: the card is the waiting screen — no
            # button at all until the worker has written the outcome
            headline, explanation = waiting_headline(order, jobs)
            return (headline, explanation, "", "", "")
        status = order.status
        if status in {Order.Status.PAID, Order.Status.PREVIEW_GENERATING}:
            return (
                "Сгенерировать превью",
                "Оплата получена. Запустите превью — клиент увидит его после вашей проверки.",
                "Сгенерировать превью",
                reverse("admin:core_order_generate_preview", args=[order.pk]),
                f"{generation_wait()} {PAID_CALL_ONE}",
            )
        if status == Order.Status.INTERNAL_PREVIEW_REVIEW:
            approved = self._approved_preview_to_send(order)
            if approved is not None:
                return (
                    "Отправить превью клиенту",
                    f"Превью #{approved.pk} одобрено. Отправьте его клиенту — заказ перейдёт в «Ждём ответ клиента».",
                    "Отправить превью клиенту",
                    reverse("admin:core_order_deliver_preview", args=[order.pk]),
                    "",
                )
            latest = self._latest_preview(order)
            if latest is None:
                return (
                    "Превью не получилось",
                    "Успешного превью нет — перегенерируйте его.",
                    "Перегенерировать превью",
                    reverse("admin:core_order_regenerate_preview", args=[order.pk]),
                    f"{generation_wait()} {PAID_CALL_ONE}",
                )
            return (
                "Проверьте превью и одобрите",
                f"Откройте превью #{latest.pk} в блоке «Превью». Годится — одобрите и отправьте клиенту; "
                "нет — «Перегенерировать превью» в блоке «Дополнительно».",
                f"Одобрить превью #{latest.pk}",
                reverse("admin:core_order_approve_preview", args=[order.pk, latest.pk]),
                "",
            )
        if status == Order.Status.PREVIEW_REVIEW:
            latest = self._latest_preview(order)
            if latest is not None and (latest.metadata or {}).get("customer_approved"):
                return (
                    "Запустить производство",
                    "Клиент одобрил превью. Запускайте производство: один стикер за нажатие, "
                    "повторяйте, пока все слоты не будут готовы.",
                    "Запустить производство",
                    reverse("admin:core_order_start_full_production", args=[order.pk]),
                    f"{generation_wait()} {PAID_CALL_PER_SLOT}",
                )
            return (
                "Ожидаем ответ клиента",
                "Превью у клиента. Ждём «Нравится» (появится кнопка производства) или запрос правки.",
                "",
                "",
                "",
            )
        if status in {Order.Status.REVISION_REQUESTED, Order.Status.REVISION_GENERATING}:
            return (
                "Сгенерировать правку",
                "Клиент попросил исправить превью (см. «Превью → Правка клиента»). "
                "Сгенерируйте новое превью с учётом правки.",
                "Сгенерировать правку",
                reverse("admin:core_order_generate_revision", args=[order.pk]),
                f"{generation_wait()} {PAID_CALL_ONE}",
            )
        if status == Order.Status.PACK_GENERATING:
            return self._production_next_step(order)
        if status == Order.Status.QUALITY_CONTROL:
            return self._qc_next_step(order)
        if status in {Order.Status.READY_FOR_DELIVERY, Order.Status.DELIVERY_IN_PROGRESS}:
            try:
                QcService().assert_delivery_allowed(order=order)
            except QcError as exc:
                return (
                    "Доставка заблокирована",
                    f"{humanize_error(exc)} Откройте блок «Контроль качества».",
                    "",
                    "",
                    "",
                )
            if status == Order.Status.READY_FOR_DELIVERY:
                return (
                    "Отправить набор клиенту",
                    "Контроль качества пройден. Отправьте стикеры клиенту в канал заказа (по 3 за нажатие).",
                    "Отправить набор клиенту",
                    reverse("admin:core_order_deliver_final", args=[order.pk]),
                    "",
                )
            return (
                "Продолжить доставку",
                "Отправлена часть набора. Нажимайте «Продолжить доставку», пока не уйдут все стикеры и финальное сообщение.",
                "Продолжить доставку",
                reverse("admin:core_order_resume_final_delivery", args=[order.pk]),
                "",
            )
        if status == Order.Status.DELIVERED:
            return ("Готово", "Набор доставлен клиенту. Действий не требуется.", "", "", "")
        if status == Order.Status.CANCELLED:
            return ("Заказ отменён", "Действий не требуется.", "", "", "")
        if status == Order.Status.FAILED:
            return ("Заказ завершён с ошибкой", "Действий в консоли нет; см. «История генераций».", "", "", "")
        return (
            "Ожидаем клиента",
            "Клиент ещё оформляет заказ в боте (фото, согласие, оплата). Действий оператора нет.",
            "",
            "",
            "",
        )

    def _production_next_step(self, order):
        pending = self.pending_retry_slots(order)
        if pending:
            names = ", ".join(slot_title(order, key) for key in pending)
            return (
                f"Перегенерировать слот {names}",
                "Контроль качества отправил этот слот на доработку — нужна новая генерация именно его; "
                "остальные стикеры сохраняются.",
                f"Перегенерировать слот {names}",
                reverse("admin:core_order_regenerate_slots", args=[order.pk]) + "?slots=" + ",".join(pending),
                f"{generation_wait()} {PAID_CALL_PER_SLOT}",
            )
        plan = self._production_plan_safe(order)
        pending_slots = any(s.status == "pending" for s in plan)
        failed = [s for s in plan if s.status == "failed" and s.retryable]
        blocked = [s for s in plan if s.status == "failed" and not s.retryable]
        if failed and not pending_slots:
            names = ", ".join(slot_title(order, s.slot_key) for s in failed)
            return (
                "Повторить неудавшиеся слоты",
                f"Слоты {names} завершились ошибкой. Повторите генерацию (по одному за нажатие).",
                "Повторить неудавшиеся слоты",
                reverse("admin:core_order_retry_failed_production", args=[order.pk]),
                f"{generation_wait()} {PAID_CALL_PER_SLOT}",
            )
        if blocked and not pending_slots:
            names = ", ".join(slot_title(order, s.slot_key) for s in blocked)
            return (
                "Слоты заблокированы",
                f"Слоты {names} заблокированы после неоднозначного ответа провайдера. "
                "Проверьте вручную и используйте «Принудительный повтор» в блоке «Дополнительно».",
                "",
                "",
                "",
            )
        done = sum(1 for s in plan if s.status == "succeeded")
        total = len(plan)
        hint = f"Готово {done} из {total}. " if total else ""
        return (
            "Запустить производство",
            f"{hint}Нажимайте «Запустить производство», пока все слоты не будут готовы; "
            "после последнего заказ перейдёт на контроль качества.",
            "Запустить производство",
            reverse("admin:core_order_start_full_production", args=[order.pk]),
            f"{generation_wait()} {PAID_CALL_PER_SLOT}",
        )

    def _qc_next_step(self, order):
        # The QC layer overrides this with the report-aware version.
        return ("Контроль качества", "Откройте QC-отчёт и заполните чек-лист.", "", "", "")

    @admin.display(description="Что делать сейчас")
    def next_step(self, order):
        if not order or not order.pk:
            return "—"
        headline, explanation, button, url, warning = self.next_step_for(order)
        parts = [format_html('<strong style="font-size:1.15em">{}</strong>', headline)]
        if explanation:
            parts.append(format_html("<span>{}</span>", explanation))
        if button and url:
            parts.append(
                format_html(
                    '<a class="button default" style="display:inline-block;margin-top:6px;'
                    'padding:8px 18px;font-size:1.05em" href="{}">{}</a>',
                    url,
                    button,
                )
            )
        if warning:
            parts.append(format_html('<em style="color:#8a5a00">{}</em>', warning))
        jobs = self.active_jobs(order)
        if jobs:
            parts.extend(self._generation_status_lines(order, jobs))
        else:
            last = latest_finished(order)
            if last is not None:
                parts.append(format_html("<span>{}</span>", last_result_text(order, last)))
        return lines_html(parts)

    def _generation_status_lines(self, order, jobs):
        """Worker health + «ждёт worker'а» + «Снять из очереди» (D-1)."""
        lines = []
        health = worker_health()
        if health is not None:
            color = "#1d6f42" if health.alive else "#a12622"
            lines.append(format_html('<span style="color:{}">{}</span>', color, health.text))
        for job in dequeue_candidates(jobs):
            lines.append(
                format_html(
                    '<span style="color:#a12622">{}</span> <a class="button" href="{}">Снять из очереди job #{}</a>',
                    queue_wait_text(job),
                    reverse("admin:core_order_dequeue_job", args=[order.pk, job.pk]),
                    job.pk,
                )
            )
        return lines

    # ------------------------------------------------- secondary actions

    def secondary_links(self, order):
        """(label, url, explanation) triples; console layers extend this."""
        links = []
        status = order.status
        if status == Order.Status.INTERNAL_PREVIEW_REVIEW:
            links.append(
                (
                    "Перегенерировать превью",
                    reverse("admin:core_order_regenerate_preview", args=[order.pk]),
                    f"Новая попытка превью, старые сохраняются. {generation_wait()} {PAID_CALL_ONE}",
                )
            )
        if status == Order.Status.PACK_GENERATING:
            links.append(
                (
                    "Повторить неудавшиеся слоты",
                    reverse("admin:core_order_retry_failed_production", args=[order.pk]),
                    f"Только слоты со статусом «ошибка», по одному за нажатие. {PAID_CALL_PER_SLOT}",
                )
            )
            links.append(
                (
                    "Перегенерировать слоты…",
                    reverse("admin:core_order_regenerate_slots", args=[order.pk]),
                    f"Новая генерация выбранных слотов (замена готовых стикеров). {PAID_CALL_PER_SLOT}",
                )
            )
            for slot in self._production_plan_safe(order):
                if slot.status == "failed" and not slot.retryable:
                    links.append(
                        (
                            f"Принудительный повтор слота {slot_title(order, slot.slot_key)}",
                            reverse("admin:core_order_force_retry_slot", args=[order.pk, slot.slot_key]),
                            "Только после ручной проверки, что генерация у провайдера не завершилась "
                            "и не была оплачена — иначе возможен дубль платной генерации.",
                        )
                    )
        if OrderCloseService.closable(order):
            links.append(
                (
                    "Закрыть заказ…",
                    reverse("admin:core_order_close", args=[order.pk]),
                    "Только суперпользователь, с причиной и пометкой по оплате. Заказ станет «Отменён» "
                    "(или «Ошибка», если платная генерация уже была); платежи, генерации и файлы сохраняются.",
                )
            )
        return links

    @admin.display(description="Второстепенные действия")
    def secondary_actions(self, order):
        if not order or not order.pk:
            return "—"
        if self.active_jobs(order):
            return "Пока идёт генерация, дополнительных действий нет."
        links = self.secondary_links(order)
        if not links:
            return "Для текущего статуса дополнительных действий нет."
        rows = [
            format_html('<a class="button" href="{}">{}</a> — {}', url, text, explanation)
            for text, url, explanation in links
        ]
        return lines_html(rows)

    # ------------------------------------------------------------ panels

    @admin.display(description="Правка клиента")
    def revision_request(self, order):
        if not order or not order.pk:
            return "—"
        try:
            revision = order.revision
        except Revision.DoesNotExist:
            return "Клиент не запрашивал правку."
        source_url = reverse("admin:core_preview_asset_file", args=[revision.source_preview_id])
        return format_html(
            "Правка #{} · {} · что исправить: <strong>{}</strong><br>"
            'исходное превью: <a href="{}" target="_blank" rel="noopener">#{}</a><br>'
            "комментарий клиента: {}",
            revision.pk,
            label(REVISION_STATUSES, revision.status),
            label(REVISION_CATEGORIES, revision.category),
            source_url,
            revision.source_preview_id,
            revision.customer_text.strip() or "—",
        )

    @admin.display(description="История генераций")
    def generation_history(self, order):
        if not order or not order.pk:
            return "—"
        jobs = order.generation_jobs.order_by("-attempt", "-created_at")
        if not jobs:
            return "Генераций пока не было."
        return format_html_join(
            LINE_BREAK,
            "<span>попытка {} · {} · {} · {}{}</span>",
            (
                (
                    job.attempt,
                    label(JOB_TASKS, job.task_type),
                    label(JOB_STATUSES, job.status),
                    job.provider,
                    f" · {job_error_text(job)}" if job.error else "",
                )
                for job in jobs
            ),
        )

    @admin.display(description="Стикеры (производство)")
    def production_plan(self, order):
        if not order or not order.pk:
            return "—"
        try:
            plan = self.get_full_production_service().production_plan(order)
        except FullProductionError:
            return "—"
        if not any(slot.attempts or slot.asset_id for slot in plan):
            return "Производство ещё не запускалось."
        storage = LocalMediaStorage()
        assets = {
            asset.pk: asset for asset in order.generated_assets.filter(kind=GeneratedAsset.Kind.FINAL)
        }
        pending_retry = set(self.pending_retry_slots(order))
        generating = bool(self.active_jobs(order))
        rows = []
        for slot in plan:
            action = ""
            if generating:
                pass  # no slot buttons while an attempt is queued / running
            elif order.status == Order.Status.PACK_GENERATING:
                if slot.slot_key in pending_retry:
                    action = format_html(
                        ' · на доработке после QC · <a class="button" href="{}">Перегенерировать</a>',
                        reverse("admin:core_order_regenerate_slots", args=[order.pk])
                        + f"?slots={slot.slot_key}",
                    )
                elif slot.status == "failed" and slot.retryable:
                    action = format_html(
                        ' · <a class="button" href="{}">Перегенерировать</a>',
                        reverse("admin:core_order_regenerate_slots", args=[order.pk])
                        + f"?slots={slot.slot_key}",
                    )
                elif slot.status == "failed" and not slot.retryable:
                    action = format_html(
                        ' · ЗАБЛОКИРОВАН · <a class="button" href="{}">Принудительный повтор</a>',
                        reverse("admin:core_order_force_retry_slot", args=[order.pk, slot.slot_key]),
                    )
            current = ""
            if slot.asset_id:
                asset = assets.get(slot.asset_id)
                dims = asset_dimensions(storage, asset) if asset else ""
                current = format_html(
                    ' · текущий файл #{}{} · <a href="{}" target="_blank" rel="noopener">Открыть</a>',
                    slot.asset_id,
                    f" · {dims}" if dims else "",
                    reverse("admin:core_preview_asset_file", args=[slot.asset_id]),
                )
            rows.append(
                format_html(
                    "Слот {} · {} · попыток: {}{}{}",
                    slot_title(order, slot.slot_key),
                    label(PRODUCTION_SLOT_STATES, slot.status),
                    slot.attempts,
                    current,
                    action,
                )
            )
        return lines_html(rows)

    @admin.display(description="Превью")
    def preview_assets(self, order):
        if not order or not order.pk:
            return "—"
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by("-created_at")
        if not assets:
            return "Превью пока нет."

        rows = []
        for asset in assets:
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            approved = bool((asset.metadata or {}).get("internal_approved"))
            approve_link = ""
            if (
                order.status == Order.Status.INTERNAL_PREVIEW_REVIEW
                and not approved
                and not self._preview_sent(asset)
                and not self.active_jobs(order)
            ):
                approve_url = reverse("admin:core_order_approve_preview", args=[order.pk, asset.pk])
                approve_link = format_html(
                    ' · <a class="button" href="{}">Одобрить это превью</a>', approve_url
                )
            rows.append(
                format_html(
                    '#{} · попытка {} · <a href="{}" target="_blank" rel="noopener">Открыть</a>{}{}',
                    asset.pk,
                    asset.job.attempt,
                    open_url,
                    " · ОДОБРЕНО" if approved else "",
                    approve_link,
                )
            )
        return lines_html(rows)

    # ------------------------------------------------------------ budget

    @admin.display(description="Вызовы/₽")
    def calls_cost(self, order):
        costs = BudgetService().order_costs(order)
        text = str(costs["calls"])
        if costs["calls"]:
            text += f" / {format_known_cost(costs['cost'])}"
        return text

    @admin.display(description="Расходы")
    def expenses(self, order):
        if not order or not order.pk:
            return "—"
        costs = BudgetService().order_costs(order)
        # the cost itself lives in «Экономика заказа» (one block, owner wording)
        parts = [
            f"вызовов: {costs['calls']} (превью {costs['preview']} / правки {costs['revision']} / "
            f"производство {costs['full']})",
            f"токенов: {costs['tokens']}",
        ]
        possibly = costs["cost"]["possibly_billable_count"]
        if possibly:
            parts.append(f"возможно платных: {possibly}")
        line = " · ".join(parts)
        slot_limit = costs["slot_limit"]
        order_limit = costs["order_limit"]
        second = f"попыток на слот: max {costs['max_attempts_per_slot']}"
        second += f" / лимит {slot_limit}" if slot_limit else " / без лимита"
        second += f" · вызовов на заказ: {costs['calls']}"
        second += f" / лимит {order_limit}" if order_limit else " / без лимита"
        return lines_html([line, second])

    @admin.display(description="Экономика заказа")
    def economics(self, order):
        """DRF-2111 PR-B: facts only — every figure comes from
        OrderEconomics.compute(); unknown parts are named, never shown as 0."""
        if not order or not order.pk:
            return "—"
        eco = OrderEconomics.compute(order)
        lines = []

        revenue = eco["revenue"]
        if revenue is None:
            lines.append("Выручка: нет подтверждённого платежа")
        elif eco["refund"]:
            refund = eco["refund"]
            lines.append(
                f"Выручка: {money(0, revenue['currency'])} — возврат "
                f"(возвращено: {money(refund['amount_minor'], refund['currency'])}, {eco['payment']['provider']})"
            )
        else:
            provider = eco["payment"]["provider"]
            lines.append(f"Выручка: {money(revenue['amount_minor'], revenue['currency'])} ({provider})")

        ai = eco["ai"]

        def _stage_text(stage):
            item = ai[stage]
            if not item["calls"]:
                return f"{STAGE_TITLES[stage]}: нет вызовов"
            text = f"{STAGE_TITLES[stage]}: {item['calls']} вызов(ов), "
            tokens = format_tokens(item.get("tokens"))
            if tokens:
                text += f"{tokens}, "
            text += format_known_cost(item)
            if item["possibly_billable_count"]:
                text += f", возможно платных: {item['possibly_billable_count']}"
            return text

        lines.append("AI по стадиям: " + " · ".join(
            _stage_text(stage) for stage in (STAGE_PREVIEW, STAGE_REVISION, STAGE_FULL, STAGE_REGENERATION)
        ))
        for stage in (STAGE_FULL, STAGE_REGENERATION):
            slots = ai[stage].get("slots") or []
            if not slots:
                continue
            title = "Слоты" if stage == STAGE_FULL else "Перегенерации"
            parts = []
            for row in slots:
                if row["cost_minor"] is not None:
                    cost = ("≈ " if row.get("estimated") else "") + money(row["cost_minor"])
                else:
                    cost = "0 ₽ (не принят)" if row["billable"] is False else "неизвестна"
                parts.append(f"{row['title'] or row['slot_key']} #{row['attempt']} — {cost}")
            lines.append(f"{title}: " + "; ".join(parts))
        lines.append(f"AI всего: {format_ai_total(ai['total'])}")
        if ai.get("pricing_caption"):
            lines.append(ai["pricing_caption"])

        manual = eco["manual"]
        if manual["rate_source"] == RATE_SOURCE_CONFIG_SNAPSHOT:
            lines.append(f"Ручная работа: {manual['minutes']} мин — {money(manual['cost_minor'])}")
        elif manual["rate_source"] == RATE_SOURCE_NOT_CONFIGURED:
            lines.append(f"Ручная работа: {manual['minutes']} мин — не настроено (ставка оператора)")
        else:
            lines.append("Ручная работа: не залогирована")

        fee = eco["payment_fee"]
        if fee["source"] == PROVIDER_CONFIRMED:
            lines.append(f"Комиссия платежа: {money(fee['amount_minor'], fee['currency'])} (по данным провайдера)")
        else:
            lines.append("Комиссия платежа: неизвестна")

        if eco["known_variable_cost_minor"] == 0 and eco["unknown_components"]:
            # nothing is known yet: naming the gaps beats printing a zero
            lines.append("Известные переменные расходы: нет известных")
        else:
            lines.append(f"Известные переменные расходы: {money(eco['known_variable_cost_minor'])}")
        contribution = eco["known_contribution_minor"]
        if contribution == NOT_COMPUTABLE:
            reason = (
                f"валюта {revenue['currency']}" if revenue is not None and revenue["currency"] != "RUB"
                else "нет выручки"
            )
            lines.append(f"Contribution: не вычисляется ({reason})")
        else:
            lines.append(f"Известный contribution: {money(contribution)}")
        if eco["unknown_components"]:
            lines.append("+ не учтено: " + "; ".join(
                unknown_component_text(code) for code in eco["unknown_components"]
            ))
        return lines_html(lines)

    def changelist_view(self, request, extra_context=None):
        summary = BudgetService().summary()

        def _line(title, item):
            text = f"{title}: {item['used']} вызовов"
            text += f" / лимит {item['max']}" if item["max"] else " / без лимита"
            if item["used"]:
                text += f", стоимость {format_known_cost(item['cost'])}"
                if item["cost"]["possibly_billable_count"]:
                    text += f" (возможно платных: {item['cost']['possibly_billable_count']})"
            return text

        extra_context = {
            **(extra_context or {}),
            "budget_lines": [_line("Сегодня", summary["today"]), _line("Месяц", summary["month"])],
            "budget_warning": any(
                item["max"] and item["used"] >= 0.8 * item["max"] for item in (summary["today"], summary["month"])
            ),
        }
        return super().changelist_view(request, extra_context=extra_context)

    def _budget_override(self, request):
        """Pilot Budget Guard (DRF-2086): the ONLY way to exceed a limit.

        Returns a BudgetOverride solely when a superuser ticked
        «Переопределить лимит» on the confirmation page and confirmed
        (POST force=1); everyone else — including a superuser without the
        explicit confirmation — gets None and is blocked by the service.
        """
        if request.user.is_superuser and request.POST.get("force") == "1":
            return BudgetOverride(actor_ref=request.user.get_username(), reason="console confirmation")
        return None

    def _budget_blocked(self, request, order, exc: BudgetError) -> None:
        """One handler for every paid action: the service refused before any
        job/provider call (its transaction rolled back); persist the
        evidence and tell the operator in Russian."""
        if isinstance(exc, BudgetExceeded):
            BudgetService.record_blocked(order, exc.decision, actor_ref=request.user.get_username())
        self.message_user(request, str(exc), level=messages.ERROR)

    @staticmethod
    def _retry_slots(order) -> list[str]:
        """Slots «Повторить неудавшиеся слоты» may run: pending QC-retry slots
        or slots whose latest FULL job failed."""
        pending = QcService().pending_retry_slots(order) if order.status == Order.Status.PACK_GENERATING else []
        if pending:
            return list(pending)
        latest = {}
        for job in order.generation_jobs.filter(task_type=GenerationJob.TaskType.FULL).order_by("slot_key", "attempt"):
            latest[job.slot_key] = job.status
        return [key for key, status in latest.items() if status == GenerationJob.Status.FAILED]

    def _budget_context(self, request, order, action, *, slot_keys=None) -> dict:
        """Confirmation-page context (informational preview of the guard):
        warning text and the override control. The service re-checks under
        the order lock on POST."""
        try:
            decision = BudgetService().check(order, action, slot_keys=slot_keys)
        except BudgetConfigError as exc:
            return {"budget_blocked": str(exc).replace(" Генерация не запущена.", ""), "budget_can_override": False}
        if decision.blocked is None:
            return {}
        return {
            "budget_blocked": decision.message.replace(" Генерация не запущена.", ""),
            "budget_can_override": request.user.is_superuser,
            "budget_force_checked": request.GET.get("force") == "1",
        }

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("user", "channel_identity", "product", "style")
            .prefetch_related("photos", "generation_jobs", "generated_assets__job", "payments", "events")
        )

    # -------------------------------------------------------------- urls

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            # DRF-2111 PR-C: «Метрики Pilot» page + §21 export (read-only)
            # DRF-2167: «Требует внимания» (read-only)
            path(
                "attention/",
                self.admin_site.admin_view(self.attention_view),
                name="core_order_attention",
            ),
            path(
                "pilot-metrics/",
                self.admin_site.admin_view(self.pilot_metrics_view),
                name="core_order_pilot_metrics",
            ),
            path(
                "pilot-metrics/export.csv",
                self.admin_site.admin_view(self.pilot_metrics_export_csv_view),
                name="core_order_pilot_metrics_export_csv",
            ),
            path(
                "pilot-metrics/export.json",
                self.admin_site.admin_view(self.pilot_metrics_export_json_view),
                name="core_order_pilot_metrics_export_json",
            ),
            path(
                "<int:order_id>/close/",
                self.admin_site.admin_view(self.close_order_view),
                name="core_order_close",
            ),
            path(
                "<int:order_id>/dequeue-job/<int:job_id>/",
                self.admin_site.admin_view(self.dequeue_job_view),
                name="core_order_dequeue_job",
            ),
            path(
                "<int:order_id>/generate-preview/",
                self.admin_site.admin_view(self.generate_preview_view),
                name="core_order_generate_preview",
            ),
            path(
                "<int:order_id>/refund-stars/",
                self.admin_site.admin_view(self.refund_stars_view),
                name="core_order_refund_stars",
            ),
            path(
                "<int:order_id>/regenerate-preview/",
                self.admin_site.admin_view(self.regenerate_preview_view),
                name="core_order_regenerate_preview",
            ),
            path(
                "<int:order_id>/generate-revision/",
                self.admin_site.admin_view(self.generate_revision_view),
                name="core_order_generate_revision",
            ),
            path(
                "<int:order_id>/approve-preview/<int:asset_id>/",
                self.admin_site.admin_view(self.approve_preview_view),
                name="core_order_approve_preview",
            ),
            path(
                "<int:order_id>/start-full-production/",
                self.admin_site.admin_view(self.start_full_production_view),
                name="core_order_start_full_production",
            ),
            path(
                "<int:order_id>/retry-failed-production/",
                self.admin_site.admin_view(self.retry_failed_production_view),
                name="core_order_retry_failed_production",
            ),
            path(
                "<int:order_id>/regenerate-slots/",
                self.admin_site.admin_view(self.regenerate_slots_view),
                name="core_order_regenerate_slots",
            ),
            path(
                "<int:order_id>/force-retry-slot/<str:slot_key>/",
                self.admin_site.admin_view(self.force_retry_slot_view),
                name="core_order_force_retry_slot",
            ),
            path(
                "preview-asset/<int:asset_id>/file/",
                self.admin_site.admin_view(self.preview_asset_file_view),
                name="core_preview_asset_file",
            ),
        ]
        return custom + urls

    # ---------------------------------------------------------- services

    def get_generation_service(self):
        # IMAGE_PROVIDER env selects the provider deterministically (default
        # openai); an experimental provider refuses personalised flows and
        # nothing falls back to OpenAI — see image_providers.get_image_provider.
        return GenerationService(provider=get_image_provider())

    def get_full_production_service(self):
        return FullProductionService(provider=get_image_provider())

    def pending_retry_slots(self, order):
        """slot_keys a FAILED QC report sent to retry whose current asset is
        still the rejected one (DRF-2079)."""
        if not order or not order.pk or order.status != Order.Status.PACK_GENERATING:
            return []
        return QcService().pending_retry_slots(order)

    def _fail(self, request, exc):
        self.message_user(request, humanize_error(exc), level=messages.ERROR)

    def _regenerate_pending_retry(self, request, *, order, service, pending, instead_of):
        """QC-retry slots are succeeded slots: start()/retry_failed() would keep
        their rejected asset and re-enter QC with the same set. Route them to
        regenerate_slots() (DRF-2051 semantics untouched)."""
        plan = service.regenerate_slots(
            order=order, slot_keys=pending, budget_override=self._budget_override(request)
        )
        names = ", ".join(slot_title(order, key) for key in pending)
        self.message_user(
            request,
            f"Контроль качества ждёт перегенерации слотов {names}: выполнена перегенерация "
            f"вместо «{instead_of}». {self._plan_message(plan, order)}",
            level=messages.WARNING,
        )
        return plan

    def _confirmation(self, request, *, order, title, action_url, detail, warning="", **extra):
        return TemplateResponse(
            request,
            "admin/core/order/preview_action_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": title,
                "order": order,
                "action_url": action_url,
                "detail": detail,
                "warning": warning,
                "opts": self.model._meta,
                **extra,
            },
        )

    def _plan_message(self, plan, order=None) -> str:
        def name(slot):
            return slot_title(order, slot.slot_key) if order is not None else slot.slot_key

        summary = ", ".join(
            f"{name(slot)}: {label(PRODUCTION_SLOT_STATES, slot.status)}" for slot in plan
        )
        hint = ""
        if any(slot.status != "succeeded" for slot in plan):
            hint = " — нажимайте «Запустить производство», пока все слоты не будут готовы."
        return f"Производство: {summary}{hint}"

    # ------------------------------------------------------------- views

    @staticmethod
    def refundable_stars_payment(order):
        """The CONFIRMED Telegram Stars payment of the order, or None."""
        return (
            order.payments.filter(provider=TelegramStarsPaymentAdapter.provider, status=Payment.Status.CONFIRMED)
            .order_by("-confirmed_at", "-pk")
            .first()
        )

    def refund_stars_view(self, request, order_id):
        """«Вернуть звёзды»: refundStarPayment for the confirmed Telegram
        Stars payment — superuser only, with a mandatory reason; at most
        once per payment (PaymentService.refund)."""
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        change_url = reverse("admin:core_order_change", args=[order.pk])
        if not request.user.is_superuser:
            self.message_user(request, "Возврат звёзд может выполнить только суперпользователь.", level=messages.ERROR)
            return redirect(change_url)
        payment = self.refundable_stars_payment(order)
        if payment is None:
            self.message_user(
                request,
                "Нет подтверждённого платежа Telegram Stars для возврата (или он уже возвращён).",
                level=messages.ERROR,
            )
            return redirect(change_url)
        action_url = reverse("admin:core_order_refund_stars", args=[order.pk])
        amount = money(payment.amount_minor, payment.currency)
        reason = " ".join(str(request.POST.get("reason") or "").split()) if request.method == "POST" else ""
        if request.method != "POST" or not reason:
            if request.method == "POST":
                self.message_user(request, "Укажите причину возврата.", level=messages.ERROR)
            return self._confirmation(
                request,
                order=order,
                title="Вернуть звёзды",
                action_url=action_url,
                detail=(
                    f"Клиенту будут возвращены {amount} (платёж #{payment.pk}, {payment.provider}). "
                    "Возврат выполняется один раз и не отменяется; статус заказа не меняется — "
                    "решение по заказу примите отдельно."
                ),
                warning="Telegram возвращает звёзды только по платежам этого бота.",
                reason_input=True,
                reason_value=reason,
                submit_label=f"Вернуть {amount}",
            )
        adapter = TelegramStarsPaymentAdapter()
        client = TelegramBotClient(os.getenv("TELEGRAM_BOT_TOKEN", ""))
        try:
            refunded = adapter.refund(
                payment=payment, client=client, actor_ref=request.user.get_username(), reason=reason,
            )
        except TelegramPaymentError as exc:
            self.message_user(request, f"Возврат не выполнен: {exc}", level=messages.ERROR)
            return redirect(change_url)
        self.message_user(
            request,
            f"Возвращено {money(refunded.amount_minor, refunded.currency)} клиенту (платёж #{refunded.pk}). "
            "Статус заказа не менялся.",
            level=messages.SUCCESS,
        )
        return redirect(change_url)

    def _report_job(self, request, job, success):
        """Outcome of a request_* call: with the inline executor the job is
        finished on return (today's messages); with the background worker it
        is queued and the card shows the progress."""
        job.refresh_from_db()
        if job.status == GenerationJob.Status.SUCCEEDED:
            asset_id = (job.output_metadata or {}).get("asset_id")
            self.message_user(request, success(asset_id, job), level=messages.SUCCESS)
        elif job.status == GenerationJob.Status.FAILED:
            error = GenerationError(job.error or f"Job #{job.pk} failed")
            error.failure = {
                key: value
                for key, value in (job.output_metadata or {}).items()
                if key != generation_queue.WORKER_KEY
            }
            self._fail(request, error)
        else:
            self.message_user(
                request,
                f"Поставлено в очередь: {label(JOB_TASKS, job.task_type)}, попытка {job.attempt} (job #{job.pk}). "
                "Страница обновится сама.",
                level=messages.SUCCESS,
            )

    def close_order_view(self, request, order_id):
        """«Закрыть заказ» (superuser): mandatory reason + money note →
        OrderCloseService (CANCELLED, or FAILED after a billable attempt)."""
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        change_url = reverse("admin:core_order_change", args=[order.pk])
        if not request.user.is_superuser:
            self.message_user(request, "Закрыть заказ может только суперпользователь.", level=messages.ERROR)
            return redirect(change_url)
        if not OrderCloseService.closable(order):
            self.message_user(
                request, f"Заказ #{order.pk} нельзя закрыть из статуса «{order.get_status_display()}».",
                level=messages.ERROR,
            )
            return redirect(change_url)
        if self._refuse_if_generating(request, order):
            return redirect(change_url)
        action_url = reverse("admin:core_order_close", args=[order.pk])
        jobs = list(order.generation_jobs.all())
        billable = OrderCloseService.had_billable_generation(jobs)
        target = "Ошибка" if billable else "Отменён"
        errors = []
        reason = request.POST.get("reason", "").strip()
        payment_note = request.POST.get("payment_note", "")
        if request.method == "POST":
            try:
                OrderCloseService.close(
                    order=order, actor_ref=request.user.get_username(), reason=reason, payment_note=payment_note,
                )
            except OrderCloseError as exc:
                errors.append(str(exc))
            else:
                order.refresh_from_db()
                self.message_user(
                    request,
                    f"Заказ #{order.pk} закрыт: «{order.get_status_display()}» "
                    f"({PAYMENT_NOTES[payment_note]}). Платежи, генерации и файлы сохранены.",
                    level=messages.SUCCESS,
                )
                return redirect(change_url)
        return self._confirmation(
            request,
            order=order,
            title=f"Закрыть заказ #{order.pk}",
            action_url=action_url,
            detail=(
                f"Заказ перейдёт в «{target}»"
                + (" — платная (или возможно платная) генерация уже была, поэтому не «Отменён»." if billable
                   else " — ни одна генерация не дошла до провайдера.")
                + " Платежи, генерации и файлы не удаляются; клиенту ничего не отправляется."
            ),
            warning="Действие необратимо: из закрытого заказа переходов нет.",
            close_form=True,
            close_reason=reason,
            close_payment_note=payment_note,
            close_payment_notes=list(PAYMENT_NOTES.items()),
            close_errors=errors,
            payments=list(order.payments.order_by("pk")),
        )

    def dequeue_job_view(self, request, order_id, job_id):
        """«Снять из очереди» (decision C-2): a PENDING job no worker picked
        up for QUEUE_WAIT_WARN_AFTER is failed as queue_lost — explicit
        operator action, never automatic."""
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        try:
            job = order.generation_jobs.get(pk=job_id)
        except GenerationJob.DoesNotExist as exc:
            raise Http404 from exc
        action_url = reverse("admin:core_order_dequeue_job", args=[order.pk, job.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Снять из очереди job #{job.pk}",
                action_url=action_url,
                detail=(
                    f"Job #{job.pk} ({label(JOB_TASKS, job.task_type)}, попытка {job.attempt}) ждёт worker'а и не был "
                    "взят в работу. Он будет помечен как неудавшийся без вызова провайдера (не оплачен); "
                    "после этого генерацию можно запустить заново."
                ),
                warning="Снимайте только если worker действительно не работает (см. индикатор в «Следующий шаг»).",
            )
        try:
            generation_queue.dequeue(job)
        except generation_queue.DequeueError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Job #{job.pk} снят из очереди — провайдер не вызывался. Можно запускать генерацию заново.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def generate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_generate_preview", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Сгенерировать превью",
                action_url=action_url,
                detail="Будет запущена новая попытка превью через настроенного провайдера изображений.",
                warning=f"{generation_wait()} {PAID_CALL_ONE}",
                **self._budget_context(request, order, "preview"),
            )
        try:
            job = self.get_generation_service().request_preview(
                order=order, budget_override=self._budget_override(request)
            )
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except GenerationError as exc:
            self._fail(request, exc)
        else:
            self._report_job(
                request, job,
                lambda asset_id, _job: f"Превью #{asset_id} сгенерировано — проверьте его и одобрите.",
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def generate_revision_view(self, request, order_id):
        """DRF-2066: run the customer's included revision (GenerationService).

        The service owns the state machine (REVISION_REQUESTED ->
        REVISION_GENERATING -> INTERNAL_PREVIEW_REVIEW) and the one-revision
        limit; this view only confirms, calls it and reports the outcome.
        """
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_generate_revision", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Сгенерировать правку",
                action_url=action_url,
                detail=(
                    "Будет сгенерировано новое превью по запросу клиента (см. блок «Правка клиента»). "
                    "Предыдущие превью сохранятся; после успеха заказ вернётся на внутреннюю проверку."
                ),
                warning=f"{generation_wait()} {PAID_CALL_ONE}",
                **self._budget_context(request, order, "revision"),
            )
        if order.status not in {Order.Status.REVISION_REQUESTED, Order.Status.REVISION_GENERATING}:
            self.message_user(
                request,
                "Правку можно сгенерировать только из статусов «Правка запрошена» / «Генерация правки».",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            job = self.get_generation_service().request_revision(
                order=order, budget_override=self._budget_override(request)
            )
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, GenerationError) as exc:
            self._fail(request, exc)
        else:
            self._report_job(
                request, job,
                lambda asset_id, job_: (
                    f"Превью с правкой #{asset_id} сгенерировано (генерация #{job_.pk}) — "
                    "проверьте и одобрите; предыдущие превью сохранены."
                ),
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def regenerate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_regenerate_preview", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Перегенерировать превью",
                action_url=action_url,
                detail="Предыдущие превью сохранятся. Будет создана новая попытка генерации.",
                warning=f"{generation_wait()} {PAID_CALL_ONE}",
                **self._budget_context(request, order, "preview"),
            )
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Перегенерация доступна только на внутренней проверке превью.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            job = self.get_generation_service().request_preview_restart(
                order=order, budget_override=self._budget_override(request)
            )
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, GenerationError) as exc:
            self._fail(request, exc)
        else:
            self._report_job(
                request, job,
                lambda asset_id, _job: f"Новое превью #{asset_id} сгенерировано; предыдущие сохранены.",
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def approve_preview_view(self, request, order_id, asset_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        try:
            asset = GeneratedAsset.objects.select_related("job").get(
                pk=asset_id, order=order, kind=GeneratedAsset.Kind.PREVIEW
            )
        except GeneratedAsset.DoesNotExist as exc:
            raise Http404 from exc

        action_url = reverse("admin:core_order_approve_preview", args=[order.pk, asset.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Одобрить превью #{asset.pk}",
                action_url=action_url,
                detail="Именно это превью будет отмечено как одобренное для отправки клиенту.",
            )

        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Одобрение доступно только на внутренней проверке превью.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if asset.job.status != GenerationJob.Status.SUCCEEDED:
            self.message_user(
                request, "Нельзя одобрить превью от неуспешной генерации.", level=messages.ERROR
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))

        for previous in order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW):
            metadata = dict(previous.metadata or {})
            changed = False
            if "internal_approved" in metadata:
                metadata.pop("internal_approved", None)
                changed = True
            if "internal_approved_at" in metadata:
                metadata.pop("internal_approved_at", None)
                changed = True
            if changed:
                previous.metadata = metadata
                previous.save(update_fields=["metadata", "updated_at"])

        metadata = dict(asset.metadata or {})
        metadata["internal_approved"] = True
        metadata["internal_approved_at"] = timezone.now().isoformat()
        asset.metadata = metadata
        asset.save(update_fields=["metadata", "updated_at"])
        try:
            OrderStateService.transition(order=order, to_status=Order.Status.PREVIEW_REVIEW)
        except InvalidOrderTransition as exc:
            self._fail(request, exc)
        else:
            self.message_user(
                request, f"Превью #{asset.pk} одобрено и ждёт ответа клиента.", level=messages.SUCCESS
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def start_full_production_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_start_full_production", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Запустить / продолжить производство",
                action_url=action_url,
                detail=(
                    "За одно нажатие генерируется один стикер (защита от таймаута). "
                    "Готовые стикеры не перегенерируются; слоты с ошибкой повторяются "
                    "через «Повторить неудавшиеся слоты»."
                ),
                warning=f"{generation_wait()} {PAID_CALL_PER_SLOT}",
                **self._budget_context(request, order, "full_start", slot_keys=self.pending_retry_slots(order) or None),
            )
        service = self.get_full_production_service()
        pending = self.pending_retry_slots(order)
        try:
            if pending:
                plan = self._regenerate_pending_retry(
                    request, order=order, service=service, pending=pending,
                    instead_of="Запустить производство",
                )
            else:
                plan = service.start(order=order, budget_override=self._budget_override(request))
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self._fail(request, exc)
        else:
            if not pending:
                self.message_user(request, self._plan_message(plan, order), level=messages.SUCCESS)
            # Production is committed; the customer notice is best-effort,
            # at most once per order, and never affects the outcome above.
            order.refresh_from_db()
            self.notify_production_started(order)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def notify_production_started(self, order):
        """MAX-only "in production" notice (DRF-2056 gap 4); other channels no-op."""
        if order.channel_identity.channel != ChannelIdentity.Channel.MAX:
            return False
        return notify_customer_production_started(
            order=order, client=MaxBotClient(os.getenv("MAX_BOT_TOKEN", ""))
        )

    def retry_failed_production_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_retry_failed_production", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Повторить неудавшиеся слоты",
                action_url=action_url,
                detail=(
                    "Повторяется только слот со статусом «ошибка» (один за нажатие). "
                    "Заблокированные слоты не затрагиваются."
                ),
                warning=f"{generation_wait()} {PAID_CALL_PER_SLOT}",
                **self._budget_context(request, order, "retry", slot_keys=self._retry_slots(order)),
            )
        service = self.get_full_production_service()
        pending = self.pending_retry_slots(order)
        try:
            if pending:
                self._regenerate_pending_retry(
                    request, order=order, service=service, pending=pending,
                    instead_of="Повторить неудавшиеся слоты",
                )
            else:
                plan = service.retry_failed(order=order, budget_override=self._budget_override(request))
                self.message_user(request, self._plan_message(plan, order), level=messages.SUCCESS)
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self._fail(request, exc)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def regenerate_slots_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_regenerate_slots", args=[order.pk])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        slot_keys_value = request.POST.get("slot_keys", request.GET.get("slots", ""))
        if request.method != "POST" and not slot_keys_value.strip():
            # Prefill from the latest FAILED QC report's pending retry_slots.
            slot_keys_value = ", ".join(self.pending_retry_slots(order))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Перегенерировать стикеры",
                action_url=action_url,
                detail=(
                    "Для каждого указанного слота будет сделана новая генерация; "
                    "новый стикер заменит текущий, старый сохранится для истории. "
                    "Остальные слоты не затрагиваются."
                ),
                warning=f"{generation_wait()} {PAID_CALL_PER_SLOT}",
                slot_input=True,
                slot_keys_value=slot_keys_value,
                slot_options=[
                    (code, slot_title(order, code))
                    for code in (order.selection or {}).get("emotions") or []
                ],
                **self._budget_context(
                    request, order, "regenerate",
                    slot_keys=[key.strip() for key in slot_keys_value.split(",") if key.strip()],
                ),
            )
        slot_keys = [key.strip() for key in slot_keys_value.split(",") if key.strip()]
        service = self.get_full_production_service()
        try:
            plan = service.regenerate_slots(
                order=order, slot_keys=slot_keys, budget_override=self._budget_override(request)
            )
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self._fail(request, exc)
        else:
            self.message_user(request, self._plan_message(plan, order), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def force_retry_slot_view(self, request, order_id, slot_key):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_force_retry_slot", args=[order.pk, slot_key])
        if self._refuse_if_generating(request, order):
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Принудительный повтор слота {slot_title(order, slot_key)}",
                action_url=action_url,
                detail=(
                    "Слот заблокирован после неоднозначного ответа провайдера. "
                    "Подтверждайте только если вы убедились, что генерация НЕ "
                    "завершилась и НЕ была оплачена — иначе возможен дубль "
                    "платной генерации."
                ),
                warning=f"{generation_wait()} {PAID_CALL_ONE}",
                **self._budget_context(request, order, "force_retry", slot_keys=[slot_key]),
            )
        service = self.get_full_production_service()
        try:
            plan = service.force_retry_slot(
                order=order, slot_key=slot_key, budget_override=self._budget_override(request)
            )
        except BudgetError as exc:
            self._budget_blocked(request, order, exc)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self._fail(request, exc)
        else:
            self.message_user(request, self._plan_message(plan, order), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def preview_asset_file_view(self, request, asset_id):
        try:
            asset = GeneratedAsset.objects.get(pk=asset_id)
        except GeneratedAsset.DoesNotExist as exc:
            raise Http404 from exc
        storage = LocalMediaStorage()
        if not storage.exists(asset.storage_key):
            raise Http404
        return FileResponse(
            storage.open(asset.storage_key),
            filename=f"preview-{asset.pk}.png",
            content_type=asset.mime_type or "application/octet-stream",
        )


class ProductionOrderPhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "status", "mime_type", "size_bytes", "created_at")
    list_filter = ("status", "mime_type")
    search_fields = ("storage_key", "original_filename")
    readonly_fields = (
        "order",
        "storage_key",
        "original_filename",
        "mime_type",
        "size_bytes",
        "status",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:photo_id>/file/",
                self.admin_site.admin_view(self.file_view),
                name="core_orderphoto_file",
            )
        ]
        return custom + urls

    def file_view(self, request, photo_id):
        try:
            photo = OrderPhoto.objects.get(pk=photo_id)
        except OrderPhoto.DoesNotExist as exc:
            raise Http404 from exc
        storage = LocalMediaStorage()
        if not storage.exists(photo.storage_key):
            raise Http404
        return FileResponse(
            storage.open(photo.storage_key),
            filename=photo.original_filename or f"photo-{photo.pk}",
            content_type=photo.mime_type or "application/octet-stream",
        )


def install_production_console():
    for model in (Order, OrderPhoto):
        try:
            admin.site.unregister(model)
        except admin.sites.NotRegistered:
            pass
    admin.site.register(Order, ProductionOrderAdmin)
    admin.site.register(OrderPhoto, ProductionOrderPhotoAdmin)
