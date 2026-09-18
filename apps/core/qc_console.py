from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.core.console_html import buttons_html, lines_html
from apps.core.console_text import (
    QC_AUTOMATED_CHECKS,
    QC_CRITERIA,
    QC_STATUSES,
    humanize_error,
    label,
    reason_label,
    slot_title,
)
from apps.core.models import Order, QcReport
from apps.core.preview_delivery_console import PreviewDeliveryOrderAdmin
from apps.core.services.order_state import InvalidOrderTransition
from apps.core.services.qc import HUMAN_CRITERIA, QcError, QcService

CHECK_OK = "ok"
CHECK_DEFECT = "defect"
DECISION_PASS = "pass"
DECISION_FAIL = "fail"


class QcOrderAdmin(PreviewDeliveryOrderAdmin):
    """Production Console QC surface (DRF-2052, UX DRF-2084).

    Оператор видит: что заказано, сколько готово, дефекты (коды причин),
    какие слоты на доработке и можно ли отправлять набор (delivery gate).
    PASS возможен только для полного комплекта; доставка заблокирована до
    QC PASS. Идентичность слота — slot_key (DRF-2051).
    """

    panel_sections = PreviewDeliveryOrderAdmin.panel_sections + (
        ("Контроль качества", "qc_panel"),
    )

    def get_qc_service(self):
        return QcService()

    # ---------------------------------------------------------- next step

    def _qc_next_step(self, order):
        report = self.get_qc_service().latest_report(order)
        if report is not None and report.status == QcReport.Status.IN_PROGRESS:
            return (
                "Заполните чек-лист контроля качества",
                f"QC-попытка {report.attempt} открыта. Оцените каждый стикер по 6 критериям "
                "и выберите «QC пройден» или «Отправить на доработку».",
                "Заполнить чек-лист",
                reverse("admin:core_order_qc_finalize", args=[order.pk]),
                "",
            )
        if report is not None and report.status == QcReport.Status.FAILED:
            return (
                "Контроль не пройден",
                "Выберите в блоке «Контроль качества» слоты для доработки («Доработать этот слот») "
                "или откройте новую QC-попытку, если оценка была ошибочной.",
                "Открыть новую QC-попытку",
                reverse("admin:core_order_qc_start", args=[order.pk]),
                "",
            )
        return (
            "Открыть контроль качества",
            "Все стикеры готовы. Откройте QC-отчёт: автоматические проверки формата пройдут сразу, "
            "затем заполните чек-лист.",
            "Открыть QC-отчёт",
            reverse("admin:core_order_qc_start", args=[order.pk]),
            "",
        )

    def secondary_links(self, order):
        links = super().secondary_links(order)
        if order.status == Order.Status.QUALITY_CONTROL:
            report = self.get_qc_service().latest_report(order)
            if report is None or report.status != QcReport.Status.IN_PROGRESS:
                links.append(
                    (
                        "Открыть новую QC-попытку",
                        reverse("admin:core_order_qc_start", args=[order.pk]),
                        "Повторяет автоматические проверки и открывает новый чек-лист "
                        "(после доработки или ошибочной оценки).",
                    )
                )
        return links

    # --------------------------------------------------------------- panel

    @admin.display(description="Контроль качества")
    def qc_panel(self, order):
        if not order or not order.pk:
            return "—"
        service = self.get_qc_service()
        expected = service.expected_asset_count(order)
        assets = service.current_final_assets(order)
        report = order.qc_reports.order_by("attempt").last()

        lines = [
            format_html("<strong>Ожидается стикеров: {} · Готово: {}</strong>", expected, len(assets))
        ]
        checks = (report.automated_checks if report else {}) or {}
        retryable = (
            report is not None
            and report.status == QcReport.Status.FAILED
            and order.status == Order.Status.QUALITY_CONTROL
        )
        retried = {slot.get("slot_key") for slot in (report.retry_slots or [])} if report else set()
        for asset in assets:
            slot_key = str(asset.slot_key)
            open_url = reverse("admin:core_preview_asset_file", args=[asset.pk])
            asset_checks = checks.get(slot_key) or {}
            failed_checks = [
                label(QC_AUTOMATED_CHECKS, name)
                for name, ok in asset_checks.items()
                if name != "asset_id" and not ok
            ]
            if asset_checks and not failed_checks:
                state = "автопроверки: OK"
            elif failed_checks:
                state = f"автопроверки: ДЕФЕКТ — {', '.join(failed_checks)}"
            else:
                state = "автопроверки ещё не выполнялись"
            retry = ""
            if retryable:
                if slot_key in retried:
                    retry = " · ДОРАБОТКА запрошена"
                else:
                    retry_url = reverse("admin:core_order_qc_retry", args=[order.pk, slot_key])
                    retry = format_html(
                        ' · <a class="button" href="{}">Доработать этот слот</a>', retry_url
                    )
            normalized = (asset.metadata or {}).get("normalized_from") or {}
            origin = ""
            if normalized:
                origin = f" · исходник {normalized.get('width')}×{normalized.get('height')} → 512"
            lines.append(
                format_html(
                    'Слот {} · файл #{} · <a href="{}" target="_blank" rel="noopener">Открыть</a> · {}{}{}',
                    slot_title(order, slot_key),
                    asset.pk,
                    open_url,
                    state,
                    origin,
                    retry,
                )
            )
        if not assets:
            lines.append("Готовых стикеров пока нет.")

        if report:
            reasons = ", ".join(reason_label(code) for code in (report.reason_codes or [])) or "—"
            lines.append(
                format_html(
                    "QC-попытка {}: <strong>{}</strong> · причины: {}",
                    report.attempt,
                    label(QC_STATUSES, report.status),
                    reasons,
                )
            )

        actions = []
        if order.status == Order.Status.QUALITY_CONTROL and (
            report is None or report.status != QcReport.Status.IN_PROGRESS
        ):
            actions.append((reverse("admin:core_order_qc_start", args=[order.pk]), "Открыть QC-отчёт"))
        if (
            order.status == Order.Status.QUALITY_CONTROL
            and report is not None
            and report.status == QcReport.Status.IN_PROGRESS
        ):
            actions.append(
                (reverse("admin:core_order_qc_finalize", args=[order.pk]), "Заполнить чек-лист")
            )
        if actions:
            lines.append(buttons_html(actions))
        pending = (
            service.pending_retry_slots(order) if order.status == Order.Status.PACK_GENERATING else []
        )
        if pending:
            lines.append(
                format_html(
                    'Ждёт перегенерации после QC: {} · <a class="button" href="{}">Перегенерировать</a>',
                    ", ".join(slot_title(order, key) for key in pending),
                    reverse("admin:core_order_regenerate_slots", args=[order.pk])
                    + "?slots="
                    + ",".join(pending),
                )
            )

        try:
            service.assert_delivery_allowed(order=order)
        except QcError as exc:
            lines.append(format_html("Доставка: заблокирована ({})", humanize_error(exc)))
        else:
            lines.append("Доставка: разрешена (QC пройден)")
        return lines_html(lines)

    # ---------------------------------------------------------------- urls

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

    # --------------------------------------------------------------- views

    def qc_start_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        action_url = reverse("admin:core_order_qc_start", args=[order.pk])
        if request.method != "POST":
            return self._confirmation(
                request,
                order=order,
                title="Открыть QC-отчёт",
                action_url=action_url,
                detail=(
                    "Стикеры будут приведены к формату мессенджера (512 px), выполнены "
                    "автоматические проверки и открыта новая QC-попытка. Затем заполните чек-лист."
                ),
            )
        try:
            report = self.get_qc_service().start_qc(order=order)
        except QcError as exc:
            self._fail(request, exc)
        else:
            self.message_user(
                request,
                f"QC-попытка {report.attempt} открыта; автоматические проверки выполнены. "
                "Заполните чек-лист.",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))

    def _checklist_context(self, request, order, report, *, answers=None, errors=None):
        service = self.get_qc_service()
        assets = {str(a.slot_key): a for a in service.current_final_assets(order)}
        checks = report.automated_checks or {}
        slots = []
        for slot_key in report.slot_keys or []:
            asset = assets.get(str(slot_key))
            slot_checks = checks.get(str(slot_key)) or {}
            failed = [
                label(QC_AUTOMATED_CHECKS, name)
                for name, ok in slot_checks.items()
                if name != "asset_id" and not ok
            ]
            slots.append(
                {
                    "key": slot_key,
                    "title": slot_title(order, slot_key),
                    "asset_id": asset.pk if asset else None,
                    "file_url": (
                        reverse("admin:core_preview_asset_file", args=[asset.pk]) if asset else ""
                    ),
                    "auto_failed": failed,
                }
            )
        criteria = [
            {"code": code, "title": title, "hint": hint, "value": (answers or {}).get(code, "")}
            for code, (title, hint) in QC_CRITERIA.items()
            if code in HUMAN_CRITERIA
        ]
        return {
            **self.admin_site.each_context(request),
            "title": f"Чек-лист контроля качества — заказ #{order.pk}",
            "order": order,
            "report": report,
            "report_status": label(QC_STATUSES, report.status),
            "expected_count_ok": bool(checks.get("expected_count")),
            "slots": slots,
            "criteria": criteria,
            "errors": errors or [],
            "action_url": reverse("admin:core_order_qc_finalize", args=[order.pk]),
            "opts": self.model._meta,
        }

    def qc_finalize_view(self, request, order_id):
        order = self.get_object(request, str(order_id))
        if order is None:
            raise Http404
        report = self._open_report(order)
        if report is None:
            self.message_user(request, "Нет открытого QC-отчёта.", level=messages.ERROR)
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        template = "admin/core/order/qc_checklist.html"
        if request.method != "POST":
            return TemplateResponse(request, template, self._checklist_context(request, order, report))

        # Explicit choice per criterion (DRF-2084): an unanswered criterion
        # is a validation error, never a silent FAIL.
        answers = {code: request.POST.get(code, "") for code in HUMAN_CRITERIA}
        decision = request.POST.get("decision", "")
        errors = []
        missing = [
            QC_CRITERIA[code][0] for code, value in answers.items() if value not in (CHECK_OK, CHECK_DEFECT)
        ]
        if missing:
            errors.append(
                "Выберите «Норма» или «Дефект» для каждого критерия: " + ", ".join(missing) + "."
            )
        defects = [QC_CRITERIA[code][0] for code, value in answers.items() if value == CHECK_DEFECT]
        if decision not in (DECISION_PASS, DECISION_FAIL):
            errors.append("Выберите решение: «QC пройден» или «Отправить на доработку».")
        elif decision == DECISION_PASS and defects:
            errors.append("«QC пройден» невозможен: отмечены дефекты — " + ", ".join(defects) + ".")
        elif decision == DECISION_FAIL and not missing and not defects:
            errors.append("«Отправить на доработку» требует хотя бы одного дефекта.")
        if errors:
            return TemplateResponse(
                request,
                template,
                self._checklist_context(request, order, report, answers=answers, errors=errors),
            )

        checklist = {code: value == CHECK_OK for code, value in answers.items()}
        try:
            result = self.get_qc_service().finalize_report(report=report, checklist=checklist)
        except (QcError, InvalidOrderTransition) as exc:
            self._fail(request, exc)
        else:
            if result.status == QcReport.Status.PASSED:
                self.message_user(request, "QC пройден — заказ готов к доставке.", level=messages.SUCCESS)
            else:
                reasons = ", ".join(reason_label(code) for code in result.reason_codes)
                self.message_user(
                    request,
                    f"QC не пройден: {reasons}. Выберите слоты для доработки в блоке «Контроль качества».",
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
                title=f"Доработать слот {slot_title(order, slot_key)}",
                action_url=action_url,
                detail=(
                    "Только этот слот будет отправлен на повторную генерацию; остальные стикеры "
                    "сохранятся. Заказ вернётся в производство — затем нажмите «Перегенерировать слот»."
                ),
            )
        report = (
            QcReport.objects.filter(order=order, status=QcReport.Status.FAILED)
            .order_by("-attempt")
            .first()
        )
        if report is None:
            self.message_user(
                request, "Нет непройденного QC-отчёта для доработки.", level=messages.ERROR
            )
            return redirect(reverse("admin:core_order_change", args=[order.pk]))
        try:
            self.get_qc_service().request_retry(report=report, slot_keys=[slot_key])
        except QcError as exc:
            self._fail(request, exc)
        else:
            self.message_user(
                request,
                f"Слот {slot_title(order, slot_key)} отправлен на доработку — "
                "нажмите «Перегенерировать слот».",
                level=messages.SUCCESS,
            )
        return redirect(reverse("admin:core_order_change", args=[order.pk]))


def install_qc_console():
    try:
        admin.site.unregister(Order)
    except admin.sites.NotRegistered:
        pass
    admin.site.register(Order, QcOrderAdmin)
