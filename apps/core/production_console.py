from django.contrib import admin, messages
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .image_providers import get_image_provider
from .models import GeneratedAsset, GenerationJob, Order, OrderPhoto, Revision
from .services.full_production import FullProductionError, FullProductionService
from .services.generation import GenerationError, GenerationService
from .services.order_state import InvalidOrderTransition, OrderStateService
from .storage import LocalMediaStorage


class ProductionOrderPhotoInline(admin.TabularInline):
    model = OrderPhoto
    extra = 0
    can_delete = False
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


class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "channel",
        "channel_identity",
        "product",
        "style",
        "status",
        "photo_count",
        "created_at",
    )
    list_filter = ("status", "channel_identity__channel", "product", "style")
    search_fields = (
        "=id",
        "channel_identity__external_user_id",
        "channel_identity__username",
        "channel_identity__display_name",
    )
    list_select_related = ("user", "channel_identity", "product", "style")
    readonly_fields = (
        "user",
        "channel_identity",
        "product",
        "style",
        "status",
        "customer_notes",
        "revision_request",
        "preview_controls",
        "generation_history",
        "production_plan",
        "preview_assets",
        "created_at",
        "updated_at",
    )
    fields = (
        "user",
        "channel_identity",
        "product",
        "style",
        "status",
        "customer_notes",
        "operator_notes",
        "revision_request",
        "preview_controls",
        "generation_history",
        "production_plan",
        "preview_assets",
        "created_at",
        "updated_at",
    )
    inlines = (ProductionOrderPhotoInline,)
    ordering = ("-created_at",)

    @admin.display(description="Канал", ordering="channel_identity__channel")
    def channel(self, order):
        return order.channel_identity.get_channel_display()

    @admin.display(description="Фото")
    def photo_count(self, order):
        return order.photos.count()

    @admin.display(description="Preview — действия")
    def preview_controls(self, order):
        if not order or not order.pk:
            return "—"
        links = []
        if order.status in {Order.Status.PAID, Order.Status.PREVIEW_GENERATING}:
            links.append(
                (
                    reverse("admin:core_order_generate_preview", args=[order.pk]),
                    "Generate / Retry Preview",
                )
            )
        if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW:
            links.append(
                (
                    reverse("admin:core_order_regenerate_preview", args=[order.pk]),
                    "Regenerate Preview",
                )
            )
        # DRF-2066: customer revision. GenerationService._start_job accepts
        # REVISION_REQUESTED (entry) and REVISION_GENERATING (retry after a
        # failed attempt), so the action is offered in both.
        if order.status in {
            Order.Status.REVISION_REQUESTED,
            Order.Status.REVISION_GENERATING,
        }:
            links.append(
                (
                    reverse("admin:core_order_generate_revision", args=[order.pk]),
                    "Generate / Retry Revision",
                )
            )
        if order.status in {Order.Status.PREVIEW_REVIEW, Order.Status.PACK_GENERATING}:
            links.append(
                (
                    reverse("admin:core_order_start_full_production", args=[order.pk]),
                    "Start / Resume Full Production",
                )
            )
        if order.status == Order.Status.PACK_GENERATING:
            links.append(
                (
                    reverse("admin:core_order_retry_failed_production", args=[order.pk]),
                    "Retry Failed Slots",
                )
            )
            links.append(
                (
                    reverse("admin:core_order_regenerate_slots", args=[order.pk]),
                    "Regenerate Slots…",
                )
            )
        if not links:
            return "Нет доступных действий для текущего статуса."
        return format_html_join(
            " &nbsp; ",
            '<a class="button" href="{}">{}</a>',
            links,
        )

    @admin.display(description="Revision (запрос клиента)")
    def revision_request(self, order):
        if not order or not order.pk:
            return "—"
        try:
            revision = order.revision
        except Revision.DoesNotExist:
            return "Клиент не запрашивал revision."
        source_url = reverse(
            "admin:core_preview_asset_file", args=[revision.source_preview_id]
        )
        return format_html(
            "Revision #{} · {} · category: <strong>{}</strong><br>"
            'source preview: <a href="{}" target="_blank" rel="noopener">#{}</a><br>'
            "customer text: {}",
            revision.pk,
            revision.get_status_display(),
            revision.get_category_display(),
            source_url,
            revision.source_preview_id,
            revision.customer_text.strip() or "—",
        )

    @admin.display(description="Generation jobs")
    def generation_history(self, order):
        if not order or not order.pk:
            return "—"
        jobs = order.generation_jobs.order_by("-attempt", "-created_at")
        if not jobs:
            return "Generation jobs пока нет."
        return format_html_join(
            "<br>",
            "<span>attempt {} · {} · {} {}</span>",
            (
                (
                    job.attempt,
                    job.get_status_display(),
                    job.provider,
                    f"· {job.error}" if job.error else "",
                )
                for job in jobs
            ),
        )

    @admin.display(description="Production plan (full generation)")
    def production_plan(self, order):
        if not order or not order.pk:
            return "—"
        try:
            plan = self.get_full_production_service().production_plan(order)
        except FullProductionError:
            return "—"
        if not any(slot.attempts or slot.asset_id for slot in plan):
            return "Full production пока не запускалась."
        rows = []
        for slot in plan:
            action = ""
            if order.status == Order.Status.PACK_GENERATING:
                if slot.status == "failed" and slot.retryable:
                    action = format_html(
                        ' · <a class="button" href="{}">Regenerate</a>',
                        reverse("admin:core_order_regenerate_slots", args=[order.pk])
                        + f"?slots={slot.slot_key}",
                    )
                elif slot.status == "failed" and not slot.retryable:
                    action = format_html(
                        ' · <a class="button" href="{}">Force retry (manual verify)</a>',
                        reverse(
                            "admin:core_order_force_retry_slot",
                            args=[order.pk, slot.slot_key],
                        ),
                    )
            rows.append(
                format_html(
                    "{} · {} · {} attempts{}{}{}",
                    slot.slot_key,
                    slot.status,
                    slot.attempts,
                    f" · current asset #{slot.asset_id}" if slot.asset_id else "",
                    "" if slot.retryable or slot.status != "failed" else " · BLOCKED",
                    action,
                )
            )
        return format_html_join("<br>", "{}", ((row,) for row in rows))

    @admin.display(description="Preview assets")
    def preview_assets(self, order):
        if not order or not order.pk:
            return "—"
        assets = order.generated_assets.filter(kind=GeneratedAsset.Kind.PREVIEW).order_by(
            "-created_at"
        )
        if not assets:
            return "Preview assets пока нет."

        rows = []
        for asset in assets:
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            approved = bool((asset.metadata or {}).get("internal_approved"))
            approve_link = ""
            if order.status == Order.Status.INTERNAL_PREVIEW_REVIEW and not approved:
                approve_url = reverse(
                    "admin:core_order_approve_preview",
                    args=[order.pk, asset.pk],
                )
                approve_link = format_html(
                    ' · <a class="button" href="{}">Approve this preview</a>',
                    approve_url,
                )
            rows.append(
                format_html(
                    '#{} · attempt {} · <a href="{}" target="_blank" rel="noopener">Открыть preview</a>{}{}',
                    asset.pk,
                    asset.job.attempt,
                    open_url,
                    " · APPROVED" if approved else "",
                    approve_link,
                )
            )
        return format_html_join("<br>", "{}", ((row,) for row in rows))

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("user", "channel_identity", "product", "style")
            .prefetch_related("photos", "generation_jobs", "generated_assets__job")
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:order_id>/generate-preview/",
                self.admin_site.admin_view(self.generate_preview_view),
                name="core_order_generate_preview",
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

    def get_generation_service(self):
        # IMAGE_PROVIDER env selects the provider deterministically (default
        # openai); an experimental provider refuses personalised flows and
        # nothing falls back to OpenAI — see image_providers.get_image_provider.
        return GenerationService(provider=get_image_provider())

    def get_full_production_service(self):
        return FullProductionService(provider=get_image_provider())

    def _confirmation(self, request, *, order, title, action_url, detail, **extra):
        return TemplateResponse(
            request,
            "admin/core/order/preview_action_confirmation.html",
            {
                **self.admin_site.each_context(request),
                "title": title,
                "order": order,
                "action_url": action_url,
                "detail": detail,
                "opts": self.model._meta,
                **extra,
            },
        )

    def generate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_generate_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Generate preview",
                action_url=action_url,
                detail="Будет запущена новая preview attempt через настроенный image provider.",
            )
        try:
            asset = self.get_generation_service().generate_preview(order=order)
        except GenerationError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Preview #{asset.pk} generated and is ready for internal review.",
                level=messages.SUCCESS,
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
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Generate revision",
                action_url=action_url,
                detail=(
                    "Будет запущена revision attempt по запросу клиента (см. блок "
                    "«Revision»). Предыдущие preview assets сохранятся; после успеха "
                    "заказ вернётся на internal preview review."
                ),
            )
        if order.status not in {
            Order.Status.REVISION_REQUESTED,
            Order.Status.REVISION_GENERATING,
        }:
            self.message_user(
                request,
                "Generate revision доступен только из REVISION_REQUESTED / REVISION_GENERATING.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            asset = self.get_generation_service().generate_revision(order=order)
        except (InvalidOrderTransition, GenerationError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Revision preview #{asset.pk} generated (job #{asset.job_id}) "
                "and is ready for internal review; previous assets were preserved.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def regenerate_preview_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_regenerate_preview", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Regenerate preview",
                action_url=action_url,
                detail="Предыдущие assets сохранятся. Будет создана новая generation attempt.",
            )
        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Regenerate доступен только на internal preview review.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PREVIEW_GENERATING,
            )
            asset = self.get_generation_service().generate_preview(order=order)
        except (InvalidOrderTransition, GenerationError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"New preview #{asset.pk} generated; previous assets were preserved.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def approve_preview_view(self, request, order_id, asset_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        try:
            asset = GeneratedAsset.objects.select_related("job").get(
                pk=asset_id,
                order=order,
                kind=GeneratedAsset.Kind.PREVIEW,
            )
        except GeneratedAsset.DoesNotExist as exc:
            raise Http404 from exc

        action_url = reverse(
            "admin:core_order_approve_preview",
            args=[order.pk, asset.pk],
        )
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Approve preview #{asset.pk}",
                action_url=action_url,
                detail="Именно этот asset будет отмечен как внутренне одобренный для отправки клиенту.",
            )

        if order.status != Order.Status.INTERNAL_PREVIEW_REVIEW:
            self.message_user(
                request,
                "Approve доступен только на internal preview review.",
                level=messages.ERROR,
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if asset.job.status != GenerationJob.Status.SUCCEEDED:
            self.message_user(
                request,
                "Нельзя одобрить asset от неуспешного generation job.",
                level=messages.ERROR,
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
            OrderStateService.transition(
                order=order,
                to_status=Order.Status.PREVIEW_REVIEW,
            )
        except InvalidOrderTransition as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Preview #{asset.pk} approved for customer review.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    @staticmethod
    def _plan_message(plan) -> str:
        summary = ", ".join(f"{slot.slot_key}: {slot.status}" for slot in plan)
        hint = ""
        if any(slot.status != "succeeded" for slot in plan):
            hint = " — запустите действие повторно, пока план не завершится."
        return f"Full production plan: {summary}{hint}"

    def start_full_production_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_start_full_production", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Start / resume full production",
                action_url=action_url,
                detail=(
                    "За один запуск обрабатывается один slot (защита от таймаута "
                    "воркера). Уже успешные slots не перегенерируются; failed slots "
                    "перезапускаются через Retry Failed Slots."
                ),
            )
        service = self.get_full_production_service()
        try:
            plan = service.start(order=order)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._plan_message(plan), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def retry_failed_production_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_retry_failed_production", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Retry failed production slots",
                action_url=action_url,
                detail=(
                    "Перезапускает только failed slots (один за запуск). "
                    "Заблокированные (ambiguous) slots не затрагиваются."
                ),
            )
        service = self.get_full_production_service()
        try:
            plan = service.retry_failed(order=order)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._plan_message(plan), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def regenerate_slots_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_regenerate_slots", args=[order.pk])
        slot_keys_value = request.POST.get(
            "slot_keys", request.GET.get("slots", "")
        )
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Regenerate production slots",
                action_url=action_url,
                detail=(
                    "Новая attempt для каждого выбранного slot (QC FAIL path); "
                    "новый asset заменяет текущий, старый сохраняется для аудита. "
                    "Остальные slots не затрагиваются."
                ),
                slot_input=True,
                slot_keys_value=slot_keys_value,
            )
        slot_keys = [
            key.strip() for key in slot_keys_value.split(",") if key.strip()
        ]
        service = self.get_full_production_service()
        try:
            plan = service.regenerate_slots(order=order, slot_keys=slot_keys)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._plan_message(plan), level=messages.SUCCESS)
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def force_retry_slot_view(self, request, order_id, slot_key):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse(
            "admin:core_order_force_retry_slot", args=[order.pk, slot_key]
        )
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Force retry slot {slot_key}",
                action_url=action_url,
                detail=(
                    "Slot заблокирован после неоднозначного ответа провайдера. "
                    "Подтверждайте только если вы убедились, что генерация НЕ "
                    "завершилась и НЕ была оплачена — иначе возможен дубль "
                    "платной генерации."
                ),
            )
        service = self.get_full_production_service()
        try:
            plan = service.force_retry_slot(order=order, slot_key=slot_key)
        except (InvalidOrderTransition, FullProductionError) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(request, self._plan_message(plan), level=messages.SUCCESS)
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
