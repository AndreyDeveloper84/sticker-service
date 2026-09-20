"""Background generation worker (async design 2026-09-20, C-1).

    python manage.py generation_worker            # forever (compose service)
    python manage.py generation_worker --burst    # drain the queue and exit (smoke / CI)

One RQ worker on the "generation" queue. Each job is executed in a forked
work-horse with job_timeout (GENERATION_JOB_TIMEOUT_S, 560 s); the parent
closes its Django DB connections before every fork so parent and child never
share a PostgreSQL socket. The job itself is generation_queue.run_job(job_id):
claim (PENDING → RUNNING) is the at-most-once gate, so a duplicated message
is harmless.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections

from apps.core.services.generation_queue import rq_connection, rq_queue

logger = logging.getLogger(__name__)


def make_worker_class():
    from rq import Worker

    class GenerationWorker(Worker):
        def execute_job(self, job, queue):
            # Parent side, right before the fork: a connection inherited by
            # the child would be closed twice (child + parent) — psycopg then
            # fails on the parent with a broken socket.
            connections.close_all()
            return super().execute_job(job, queue)

    return GenerationWorker


class Command(BaseCommand):
    help = "Run the background generation worker (RQ queue 'generation')."

    def add_arguments(self, parser):
        parser.add_argument("--burst", action="store_true", help="process queued jobs and exit")
        parser.add_argument("--name", default="", help="worker name (default: generation@<host>.<pid>)")

    def handle(self, *args, **options):
        import os
        import socket

        connection = rq_connection()
        queue = rq_queue(connection)
        name = options["name"] or f"generation@{socket.gethostname()}.{os.getpid()}"
        worker = make_worker_class()(
            [queue],
            connection=connection,
            name=name,
            default_result_ttl=0,
        )
        self.stdout.write(
            f"generation_worker name={name} queue={settings.GENERATION_QUEUE_NAME} "
            f"job_timeout={settings.GENERATION_JOB_TIMEOUT_S}s burst={options['burst']}"
        )
        worker.work(burst=options["burst"], with_scheduler=False)
