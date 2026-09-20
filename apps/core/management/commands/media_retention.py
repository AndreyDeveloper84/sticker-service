"""``manage.py media_retention`` — customer media retention (DRF-2170).

Dry-run by default: prints the plan (orders, kinds, files, bytes) and
deletes nothing. ``--apply`` deletes the planned files — only when
``MEDIA_RETENTION_ENABLED=true``; otherwise the command refuses.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.media_lifecycle import KINDS, MediaLifecycleError, MediaLifecycleService


class Command(BaseCommand):
    help = "Delete media of terminal orders older than the retention periods (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="delete the planned files (requires MEDIA_RETENTION_ENABLED=true)")
        parser.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")

    def handle(self, *args, **options):
        service = MediaLifecycleService()
        plan = service.retention_plan()
        policy = plan.policy
        days = ", ".join(f"{kind}={policy['days'][kind] if policy['days'][kind] is not None else 'keep'}" for kind in KINDS)
        self.stdout.write(f"retention: enabled={policy['enabled']} · {days}")
        counts = plan.counts()
        self.stdout.write(
            f"plan: {len(plan.orders)} order(s), " + ", ".join(f"{kind} {counts[kind]}" for kind in KINDS)
            + f", {plan.bytes} bytes"
        )
        for order in plan.orders:
            kinds = sorted({item.kind for item in plan.items if item.order.pk == order.pk})
            self.stdout.write(f"  order #{order.pk} ({order.status}): {', '.join(kinds)}")
        if not options["apply"]:
            self.stdout.write("dry-run: nothing deleted (use --apply with MEDIA_RETENTION_ENABLED=true)")
            return
        try:
            report = service.apply_retention(plan)
        except MediaLifecycleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"applied: {report['orders']} order(s), {report['files']} file(s), {report['bytes']} bytes deleted"
            + (f", {report['errors']} file system error(s) — see the log" if report["errors"] else "")
        )
