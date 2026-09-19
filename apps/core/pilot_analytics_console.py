"""«Метрики Pilot» page and §21 export on the Production Console (DRF-2111, PR-C).

Every figure is prepared here from ``PilotAnalyticsService.snapshot()`` —
templates only print rows. Rendering rules follow PR-B: unknown values are
«неизвестна» / «не настроено», never 0; RUB and XTR never share a cell.
"""

from __future__ import annotations

import csv
from datetime import date

from django.http import HttpResponse, JsonResponse
from django.template.response import TemplateResponse

from .services.generation_cost import format_known_cost
from .services.order_economics import STAGE_TITLES, money
from .services.pilot_analytics import EXPORT_COLUMNS, PRESETS, PilotAnalyticsService, period_for

FUNNEL_TITLES = {
    "start": "Заказ создан",
    "payment": "Оплата",
    "preview": "Превью готово",
    "approval": "Превью одобрено",
    "delivered": "Доставлен",
}
UNKNOWN_TITLES = {
    "payment_fee_unknown": "комиссия неизвестна",
    "ai_unknown_price": "AI без цены",
    "ai_possibly_billable": "возможно платные AI",
    "manual_rate_not_configured": "ставка не настроена",
    "revenue_currency_xtr": "выручка в XTR",
    "no_confirmed_payment": "нет платежа",
}


def parse_period(request):
    preset = request.GET.get("preset") or "today"
    if preset not in PRESETS:
        preset = "today"
    start = end = None
    if preset == "custom":
        try:
            start = date.fromisoformat(request.GET.get("start") or "")
            end = date.fromisoformat(request.GET.get("end") or "")
        except ValueError:
            start = end = None
        if start is None or end is None:
            preset = "30d"
    return period_for(preset, start=start, end=end)


def _num(value, suffix=""):
    return "—" if value is None else f"{value:g}{suffix}"


def _percent(value):
    return "—" if value is None else f"{value:g} %"


def _per_currency(items):
    """[{"currency", "orders", "amount_minor"}] → «100,00 ₽ (2)» · «460 XTR (1)»."""
    if not items:
        return "—"
    return " · ".join(f"{money(item['amount_minor'], item['currency'])} ({item['orders']})" for item in items)


def _stage_line(stage, item):
    if not item["calls"]:
        return f"{STAGE_TITLES[stage]}: нет вызовов"
    text = f"{STAGE_TITLES[stage]}: {item['calls']} вызов(ов), {format_known_cost(item)}"
    if item["possibly_billable_count"]:
        text += f", возможно платных: {item['possibly_billable_count']}"
    if item["unknown_price_count"]:
        text += f", без цены: {item['unknown_price_count']}"
    return text


def _per_paid_order(ai, value):
    """avg / median over paid orders with ≥ 1 call and a fully known cost;
    none of them → говорим об этом, а не печатаем 0."""
    known = ai["paid_orders_with_known_cost"]
    if not known or value is None:
        return "неизвестна (нет заказов с известной стоимостью)"
    return f"{money(int(round(value)))} (по {known} из {ai['paid_orders']} оплаченных с полностью известной стоимостью)"


def _unit_ai(item):
    ai = item["ai"]
    text = format_known_cost(ai)
    unknown_jobs = ai["jobs"] - ai["known_count"] - ai["not_billable_count"]
    if ai["known_count"] and unknown_jobs:
        text += f" (+ неизвестно: {unknown_jobs})"
    return text


def _unit_manual(item):
    manual = item["manual"]
    if not manual["orders_with_logs"]:
        return "нет логов"
    priced = manual["orders_with_logs"] - manual["orders_not_configured"]
    if not priced:
        return "не настроено"
    text = money(item["known_manual_cost_minor"])
    if manual["orders_not_configured"]:
        text += f" (не настроено для {manual['orders_not_configured']})"
    return text


def _unit_fee(item):
    fee = item["payment_fee"]
    if not fee["orders_known"] and not fee["orders_unknown"]:
        return "нет платежей"
    if not fee["orders_known"]:
        return "неизвестна"
    text = money(item["known_payment_fee_minor"])
    if fee["orders_unknown"]:
        text += f" (неизвестна для {fee['orders_unknown']})"
    return text


def _unit_contribution(item):
    if item["known_contribution_minor"] is None:
        return "не вычисляется"
    text = f"{money(item['known_contribution_minor'])} (по {item['contribution_orders']} заказам"
    if item["orders_with_unknown"]:
        text += f", не учтено у {item['orders_with_unknown']}"
    return text + ")"


def build_context(snapshot: dict) -> dict:
    orders = snapshot["orders"]
    funnel = snapshot["funnel"]
    revenue = snapshot["revenue"]
    ai = snapshot["ai_cost"]
    quality = snapshot["quality"]
    ops = snapshot["operations"]
    budget = snapshot["budget"]

    funnel_rows = [
        (FUNNEL_TITLES[step["step"]], step["count"], _percent(step["conversion_percent"]))
        for step in funnel["steps"]
    ]
    revenue_rows = [
        (cell["product"], cell["channel"], cell["currency"], money(cell["amount_minor"], cell["currency"]), cell["orders"])
        for cell in revenue["cells"]
    ]
    ai_rows = [
        ("Всего", format_known_cost(ai["total"])
            + (f", возможно платных: {ai['total']['possibly_billable_count']}" if ai["total"]["possibly_billable_count"] else "")
            + (f", без цены: {ai['total']['unknown_price_count']}" if ai["total"]["unknown_price_count"] else "")),
        ("По стадиям", " · ".join(_stage_line(stage, ai["stages"][stage]) for stage in ai["stages"])),
        ("Средняя на оплаченный заказ", _per_paid_order(ai, ai["known_cost_minor_per_paid_order_avg"])),
        ("Медиана на оплаченный заказ", _per_paid_order(ai, ai["known_cost_minor_per_paid_order_median"])),
    ]
    quality_rows = [
        ("Превью принято с первой попытки",
         f"{quality['preview_first_try_accepted']} из {quality['orders_with_preview']} ({_percent(quality['preview_first_try_percent'])})"),
        ("Правок на заказ", f"{_num(quality['revisions_per_order'])} ({quality['orders_with_revision']} заказов, {quality['revision_jobs']} генераций)"),
        ("Перегенераций на оплаченный заказ", f"{_num(quality['regenerations_per_paid_order'])} ({quality['regeneration_calls']} вызовов)"),
        ("Отказы модерации", quality["moderation_failures"]),
        ("QC: отправлено на перегенерацию", quality["qc_retries"]),
    ]
    ops_rows = [
        ("Ручная работа на заказ с логами", f"{_num(ops['manual_minutes_per_logged_order'], ' мин')} ({ops['orders_with_manual_logs']} заказов, всего {ops['manual_minutes_total']} мин)"),
        ("Ручная работа на оплаченный заказ", f"{_num(ops['manual_minutes_per_paid_order'], ' мин')}; без логов: {ops['paid_orders_without_logs']}"),
        ("Оплата → доставка, медиана", f"{_num(ops['payment_to_delivery_hours_median'], ' ч')} (по {ops['lead_time_orders']} заказам; среднее {_num(ops['payment_to_delivery_hours_avg'], ' ч')})"),
    ]
    unit_rows = []
    for item in snapshot["unit_economics"]:
        unknown = "; ".join(
            f"{UNKNOWN_TITLES.get(code, code)}: {count}" for code, count in sorted(item["unknown_counts"].items())
        )
        price = money(item["price_minor"], item["price_currency"]) if item["price_minor"] is not None else "—"
        unit_rows.append({
            "product": item["product"],
            "name": item["name"],
            "active": item["is_active"],
            "price": price,
            "orders": item["orders"],
            "paid": item["paid_orders"],
            "revenue": _per_currency(item["revenue"]),
            "ai": _unit_ai(item),
            "manual": _unit_manual(item),
            "fee": _unit_fee(item),
            "contribution": _unit_contribution(item),
            "unknown": f"{item['orders_with_unknown']} заказ(ов): {unknown}" if item["orders_with_unknown"] else "—",
        })

    def _budget_row(title, item):
        limit = f"лимит {item['max']}" if item["max"] else "без лимита"
        text = f"{item['used']} вызовов / {limit}"
        if item["utilization_percent"] is not None:
            text += f" ({item['utilization_percent']:g} %)"
        if item["used"]:
            text += f", стоимость {format_known_cost(item['cost'])}"
        return {"title": title, "text": text, "warning": item["warning"], "limit_reached": item["limit_reached"]}

    budget_rows = [_budget_row("Сегодня", budget["today"]), _budget_row("Месяц", budget["month"])]
    limits = (
        f"на заказ: {budget['order_limit'] or 'без лимита'} · на слот: {budget['slot_limit'] or 'без лимита'} · "
        f"тариф: {money(round(budget['cost_rub_per_call'] * 100)) if budget['cost_rub_per_call'] is not None else 'не задан'}"
    )
    return {
        "orders_rows": [
            ("Создано", orders["started"]), ("Оплачено", orders["paid"]), ("Доставлено", orders["delivered"]),
            ("Отменено", orders["cancelled"]), ("Ошибка", orders["failed"]),
        ],
        "funnel_rows": funnel_rows,
        "funnel_overall": _percent(funnel["overall_percent"]),
        "revenue_rows": revenue_rows,
        "revenue_totals": _per_currency(revenue["totals"]),
        "ai_rows": ai_rows,
        "quality_rows": quality_rows,
        "ops_rows": ops_rows,
        "unit_rows": unit_rows,
        "budget_rows": budget_rows,
        "budget_limits": limits,
        "budget_warning": budget["warning"],
        "budget_limit_reached": budget["limit_reached"],
    }


class PilotAnalyticsViews:
    """Mixed into ProductionOrderAdmin; wired in get_urls."""

    def pilot_metrics_view(self, request):
        period = parse_period(request)
        service = PilotAnalyticsService(period)
        snapshot = service.snapshot()
        context = {
            **self.admin_site.each_context(request),
            "title": "Метрики Pilot",
            "period": period,
            "presets": (("today", "Сегодня"), ("7d", "7 дней"), ("30d", "30 дней"), ("custom", "Период")),
            "query": request.GET.urlencode(),
            **build_context(snapshot),
        }
        return TemplateResponse(request, "admin/core/order/pilot_metrics.html", context)

    def pilot_metrics_export_csv_view(self, request):
        period = parse_period(request)
        rows = PilotAnalyticsService(period).export_rows()
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="pilot-metrics_{period.label}.csv"'
        writer = csv.DictWriter(response, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if row[key] is None else row[key]) for key in EXPORT_COLUMNS})
        return response

    def pilot_metrics_export_json_view(self, request):
        period = parse_period(request)
        rows = PilotAnalyticsService(period).export_rows()
        payload = {"period": period.as_dict(), "columns": list(EXPORT_COLUMNS), "rows": rows}
        response = JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})
        response["Content-Disposition"] = f'attachment; filename="pilot-metrics_{period.label}.json"'
        return response

