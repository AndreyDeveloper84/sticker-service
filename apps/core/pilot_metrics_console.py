"""Admin surface for the pilot metrics log (DRF-2055).

OrderEvent is append-only: the log itself is read-only in the admin. The one
thing operators enter by hand is manual work time, through the
"Manual work log" proxy, so Manual Minutes per Order is measured explicitly
instead of being inferred from wall-clock gaps between console actions.
"""

from django import forms
from django.contrib import admin

from apps.core.models import ManualWorkLog, OrderEvent
from apps.core.services.order_economics import RATE_KEY, manual_work_snapshot


class ManualWorkLogForm(forms.ModelForm):
    minutes = forms.IntegerField(min_value=1, max_value=24 * 60, help_text="Operator minutes actually spent on this order.")
    activity = forms.ChoiceField(choices=OrderEvent.Activity.choices)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = ManualWorkLog
        fields = ("order",)

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.event_type = OrderEvent.Type.MANUAL_WORK_LOGGED
        instance.actor_kind = OrderEvent.Actor.OPERATOR
        instance.payload = {
            "minutes": self.cleaned_data["minutes"],
            "activity": self.cleaned_data["activity"],
            "note": (self.cleaned_data.get("note") or "").strip(),
        }
        # DRF-2111: operator rate snapshot at logging time (immutable); no
        # configured rate → no snapshot → the log's cost stays "не настроено".
        rate = manual_work_snapshot(self.cleaned_data["minutes"])
        if rate is not None:
            instance.payload[RATE_KEY] = rate
        if commit:
            instance.save()
        return instance


@admin.register(ManualWorkLog)
class ManualWorkLogAdmin(admin.ModelAdmin):
    form = ManualWorkLogForm
    raw_id_fields = ("order",)
    list_display = ("id", "order", "minutes", "activity", "actor_ref", "created_at")
    list_filter = ("created_at",)
    search_fields = ("order__id", "actor_ref")
    ordering = ("-created_at",)

    def get_fields(self, request, obj=None):
        return ("order", "minutes", "activity", "note")

    def minutes(self, event):
        return (event.payload or {}).get("minutes")

    def activity(self, event):
        return (event.payload or {}).get("activity")

    def save_model(self, request, obj, form, change):
        obj.actor_ref = request.user.get_username()
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# Event types that live outside OrderEvent.Type on purpose (no choices
# migration during the pilot, DRF-2086); the filter still has to offer them.
EXTRA_EVENT_TYPES = (
    (OrderEvent.BUDGET_BLOCKED, "Бюджет: действие заблокировано"),
    (OrderEvent.BUDGET_OVERRIDE, "Бюджет: лимит переопределён"),
    (OrderEvent.BUDGET_ALERT, "Бюджет: алерт"),
    (OrderEvent.PAYMENT_REFUNDED, "Платёж возвращён"),
)


class EventTypeFilter(admin.SimpleListFilter):
    title = "тип события"
    parameter_name = "event_type"

    def lookups(self, request, model_admin):
        return [*OrderEvent.Type.choices, *EXTRA_EVENT_TYPES]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(event_type=self.value())
        return queryset


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "event_type", "from_status", "to_status", "actor_kind", "actor_ref", "created_at")
    list_filter = (EventTypeFilter, "actor_kind", "to_status")
    search_fields = ("order__id", "actor_ref")
    ordering = ("-created_at",)
    readonly_fields = ("order", "event_type", "from_status", "to_status", "actor_kind", "actor_ref", "payload", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
