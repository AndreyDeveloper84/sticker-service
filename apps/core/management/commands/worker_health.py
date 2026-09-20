"""Health probe for the background generation worker (compose healthcheck).

    python manage.py worker_health --max-age 90

Exit 0 when at least one RQ worker listening on the "generation" queue has
sent a heartbeat within --max-age seconds; exit 1 otherwise (no worker, dead
worker, Redis unreachable). Prints one line with the facts.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand

from apps.core.services.generation_queue import rq_connection, rq_queue


class Command(BaseCommand):
    help = "Exit 0 if a live generation worker (recent heartbeat) is attached to the queue."

    def add_arguments(self, parser):
        parser.add_argument("--max-age", type=int, default=90, help="max heartbeat age in seconds")

    def handle(self, *args, **options):
        from rq import Worker

        try:
            connection = rq_connection()
            queue = rq_queue(connection)
            workers = Worker.all(queue=queue)
        except Exception as exc:  # redis down → unhealthy
            self.stderr.write(f"worker_health: redis error: {exc}")
            raise SystemExit(1)
        now = datetime.now(dt_timezone.utc)
        alive = []
        for worker in workers:
            beat = worker.last_heartbeat
            if beat is None:
                continue
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=dt_timezone.utc)
            age = (now - beat).total_seconds()
            if age <= options["max_age"]:
                alive.append(f"{worker.name} state={worker.get_state()} heartbeat_age={int(age)}s")
        if not alive:
            self.stderr.write(f"worker_health: no live worker on queue '{queue.name}' (workers seen: {len(workers)})")
            raise SystemExit(1)
        self.stdout.write(f"worker_health: ok queued={queue.count} " + "; ".join(alive))
