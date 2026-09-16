from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from apps.core.models import Order, QcReport
from apps.core.preview_delivery_console import PreviewDeliveryOrderAdmin
from apps.core.services.order_state import InvalidOrderTransition
from apps.core.services.qc import (
    HUMAN_CRITERIA,
    QUALITY_CONTROL,
    QcError,
    QcService,
)


class QcOrderAdmin(PreviewDeliveryOrderAdmin):
    """Production Console QC surface (DRF-2052).

    Оператор видит: что заказано (expected), сколько готово (generated),
    дефект (reason codes), какие slot_key на retry, можно ли deliver
    (delivery gate). PASS возможен только для полного комплекта; delivery
    заблокирован до QC PASS. Canonical slot identity — slot_key (DRF-2051).
    """

    readonly_fields = PreviewDeliveryOrderAdmin.readonly_fields + ("qc_panel",)
    fields = PreviewDeliveryOrderAdmin.fields + ("qc_panel",)

    def get_qc_service(self):
        return QcService()

    @admin.display(description="QC — финальные стикеры")
    def qc_panel(self, order):
        if not order or not order.pk:
            return "—"
        service = self.get_qc_service()
        expected = service.expected_asset_count(order)
        assets = service.current_final_assets(order)
        report = order.qc_reports.order_by("attempt").last()

        lines = [
            format_html(
                "<strong>Ожидается: {} · Готово: {}</strong>",
                expected,
                len(assets),
            )
        ]
        checks = (report.automated_checks if report else {}) or {}
        retryable = (
            report is not None
            and report.status == QcReport.Status.FAILED
            and order.status == QUALITY_CONTROL
        )
        retried = (
            {slot.get("slot_key") for slot in (report.retry_slots or [])}
            if report
            else set()
        )
        for asset in assets:
            slot_key = str(asset.slot_key)
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            asset_checks = checks.get(slot_key) or {}
            failed_checks = [
                name
                for name, ok in asset_checks.items()
                if name != "asset_id" and not ok
            ]
            if asset_checks and not failed_checks:
                state = "OK"
            elif failed_checks:
                state = f"AUTO FAIL: {', '.join(failed_checks)}"
            else:
                state = "нет auto-checks"
            retry = ""
            if retryable:
                if slot_key in retried:
                    retry = " · RETRY запрошен"
                else:
                    retry_url = reverse(
                        "admin:core_order_qc_retry", args=[order.pk, slot_key]
                    )
                    retry = format_html(
                        ' · <a class="button" href="{}">Retry этот slot</a>', retry_url
                    )
            lines.append(
                format_html(
                    'slot {} · #{} · <a href="{}" target="_blank" rel="noopener">Открыть</a> · {}{}',
                    slot_key,
                    asset.pk,
                    open_url,
                    state,
                    retry,
                )
            )
        if not assets:
            lines.append("Финальных assets пока нет.")

        if report:
            reasons = ", ".join(report.reason_codes or []) or "—"
            lines.append(
                format_html(
                    "QC attempt {}: <strong>{}</strong> · reasons: {}",
                    report.attempt,
                    report.get_status_display(),
                    reasons,
                )
            )

        actions = []
        if order.status == QUALITY_CONTROL and (
            report is None or report.status != QcReport.Status.IN_PROGRESS
        ):
            actions.append(
                (
                    reverse("admin:core_order_qc_start", args=[order.pk]),
                    "Открыть QC report",
                )
            )
        if (
            order.status == QUALITY_CONTROL
            and report is not None
            and report.status == QcReport.Status.IN_PROGRESS
        ):
            actions.append(
                (
                    reverse("admin:core_order_qc_finalize", args=[order.pk]),
                    "Завершить QC (checklist)",
                )
            )
        if actions:
            lines.append(
                format_html_join(
                    " &nbsp; ", '<a class="button" href="{}">{}</a>', actions
                )
            )

        try:
            service.assert_delivery_allowed(order=order)
        except QcError as exc:
            lines.append(format_html("DELIVERY: заблокирован ({})", exc))
        else:
            lines.append("DELIVERY: разрешён (QC PASS)")
        return format_html_join("<br>", "{}", ((line,) for line in lines))

    def get_urls(self):
        custom = [
            path(
                "<int:order_id>/qc/start/",
                self.admin_site.admin_view(self.qc_start_view),
                name="core_order_qc_start",
            ),
            path(
                "<int:order_id>/qc/finalize/",
                self.admin_site.admin_view(self.qc_finalize_view),
                name="core_order_qc_finalize",
            ),
            path(
                "<int:order_id>/qc/retry/<str:slot_key>/",
                self.admin_site.admin_view(self.qc_retry_view),
                name="core_order_qc_retry",
            ),
        ]
        return custom + super().get_urls()

    def _open_report(self, order):
        return (
            QcReport.objects.filter(order=order, status=QcReport.Status.IN_PROGRESS)
            .order_by("-attempt")
            .first()
        )

    def qc_start_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_qc_start", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Открыть QC report",
                action_url=action_url,
                detail="Будут выполнены автоматические проверки формата финальных assets и открыта новая QC attempt.",
            )
        try:
            report = self.get_qc_service().start_qc(order=order)
        except QcError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"QC attempt {report.attempt} открыт; automated checks записаны.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def qc_finalize_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        report = self._open_report(order)
        if report is None:
            self.message_user(
                request, "Нет открытого QC report.", level=messages.ERROR
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        if request.method != "POST":
            return TemplateResponse(
                request,
                "admin/core/order/qc_checklist.html",
                {
                    **self.admin_site.each_context(request),
                    "title": f"QC checklist — order #{order.pk}",
                    "order": order,
                    "report": report,
                    "criteria": HUMAN_CRITERIA,
                    "automated_checks": report.automated_checks or {},
                    "action_url": reverse(
                        "admin:core_order_qc_finalize", args=[order.pk]
                    ),
                    "opts": self.model._meta,
                },
            )
        checklist = {
            criterion: bool(request.POST.get(criterion))
            for criterion in HUMAN_CRITERIA
        }
        try:
            result = self.get_qc_service().finalize_report(
                report=report, checklist=checklist
            )
        except (QcError, InvalidOrderTransition) as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            if result.status == QcReport.Status.PASSED:
                self.message_user(
                    request,
                    "QC PASS — заказ готов к delivery.",
                    level=messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    f"QC FAIL: {', '.join(result.reason_codes)}. Выберите slots для retry.",
                    level=messages.WARNING,
                )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def qc_retry_view(self, request, order_id, slot_key):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_qc_retry", args=[order.pk, slot_key])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title=f"Retry slot {slot_key}",
                action_url=action_url,
                detail="Только выбранный slot_key будет отправлен на повторную генерацию (DRF-2051); остальные slots сохранятся.",
            )
        report = (
            QcReport.objects.filter(order=order, status=QcReport.Status.FAILED)
            .order_by("-attempt")
            .first()
        )
        if report is None:
            self.message_user(
                request, "Нет FAILED QC report для retry.", level=messages.ERROR
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            self.get_qc_service().request_retry(report=report, slot_keys=[slot_key])
        except QcError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"Slot {slot_key} отправлен на selective retry.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))


def install_qc_console():
    try:
        admin.site.unregister(Order)
    except admin.sites.NotRegistered:
        pass
    admin.site.register(Order, QcOrderAdmin)
