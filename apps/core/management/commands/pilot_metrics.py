import json
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.services.pilot_metrics import PilotMetricsService


def _parse_day(value: str, *, end: bool = False):
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}, expected YYYY-MM-DD") from exc
    return timezone.make_aware(datetime.combine(day, time.max if end else time.min))


class Command(BaseCommand):
    help = "Print the pilot metrics snapshot (DRF-2055): funnel, drop-off, payments, previews, cost, manual work."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Orders created on/after this day (YYYY-MM-DD)")
        parser.add_argument("--until", help="Orders created on/before this day (YYYY-MM-DD)")
        parser.add_argument("--json", action="store_true", help="Emit a single JSON document")

    def handle(self, *args, **options):
        since = _parse_day(options["since"]) if options.get("since") else None
        until = _parse_day(options["until"], end=True) if options.get("until") else None
        snapshot = PilotMetricsService(since=since, until=until).snapshot()

        if options["json"]:
            self.stdout.write(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
            return

        for section, values in snapshot.items():
            self.stdout.write(self.style.MIGRATE_HEADING(section))
            for key, value in values.items():
                self.stdout.write(f"  {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
