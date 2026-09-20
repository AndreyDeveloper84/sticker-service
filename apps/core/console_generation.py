"""Async-aware order card (async design 2026-09-20, D-1).

The console never waits for the provider any more: a click creates the job
and returns; this module turns the job rows into what the operator sees —
«в очереди (job #N)» / «генерируется… N с», the «Последняя генерация: …»
line, the worker health indicator and the «Снять из очереди» offer. Only
reads GenerationJob (plus one Redis call for the worker heartbeat when the
background worker is enabled); every state change stays in the services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from apps.core.console_text import JOB_TASKS, job_error_text, label, slot_title
from apps.core.models import GenerationJob
from apps.core.services.generation import STALE_RUNNING_AFTER
from apps.core.services.generation_queue import (
    QUEUE_LOST,
    QUEUE_WAIT_WARN_AFTER,
    WORKER_KEY,
    queue_wait,
    worker_enabled,
)

# «Последняя генерация: …» is shown this long after the job finished; older
# outcomes are history («История генераций»), not news.
LAST_RESULT_WINDOW = timedelta(minutes=30)

# The worker is «alive» when its RQ heartbeat is at most this old (the
# compose healthcheck uses the same figure: worker_health --max-age 90).
WORKER_HEARTBEAT_MAX_AGE = timedelta(seconds=90)

GENERATING_HINT = "Страница обновляется каждые 10 с; её можно закрыть — результат появится здесь же."


def active_jobs(order) -> list[GenerationJob]:
    """Jobs the operator must wait for: PENDING (queued) and RUNNING that is
    not stale (older RUNNING jobs are reaped on the card GET). Ordered by pk."""
    stale_before = timezone.now() - STALE_RUNNING_AFTER
    jobs = []
    for job in order.generation_jobs.all():
        if job.status == GenerationJob.Status.PENDING:
            jobs.append(job)
        elif job.status == GenerationJob.Status.RUNNING:
            started = job.started_at or job.created_at
            if started and started >= stale_before:
                jobs.append(job)
    return sorted(jobs, key=lambda j: j.pk)


def picked_at(job: GenerationJob):
    return ((job.output_metadata or {}).get(WORKER_KEY) or {}).get("picked_at")


def elapsed_seconds(job: GenerationJob, now=None) -> int:
    since = job.started_at or job.created_at
    if since is None:
        return 0
    return max(0, int(((now or timezone.now()) - since).total_seconds()))


def job_title(order, job: GenerationJob) -> str:
    """«превью» / «правка» / «производство, слот «Спасибо»»."""
    text = label(JOB_TASKS, job.task_type)
    if job.task_type == GenerationJob.TaskType.FULL and job.slot_key:
        text += f", слот {slot_title(order, job.slot_key)}"
    return text


def waiting_headline(order, jobs: list[GenerationJob]) -> tuple[str, str]:
    """(headline, explanation) of «Следующий шаг» while ``jobs`` are active."""
    running = [job for job in jobs if job.status == GenerationJob.Status.RUNNING]
    pending = [job for job in jobs if job.status == GenerationJob.Status.PENDING]
    if running:
        job = running[0]
        headline = f"Генерируется {job_title(order, job)}… job #{job.pk} · {elapsed_seconds(job)} с"
    else:
        job = pending[0]
        headline = f"В очереди: {job_title(order, job)} (job #{job.pk})"
    parts = [f"попытка {job.attempt}"]
    if len(jobs) > 1:
        parts.append(f"ещё в очереди: {len(jobs) - 1}")
    return headline, " · ".join(parts) + ". " + GENERATING_HINT


def queue_wait_text(job: GenerationJob) -> str:
    """«ждёт worker'а N мин» for a PENDING job past the warn threshold, else ''."""
    waited = queue_wait(job)
    if waited is None or waited < QUEUE_WAIT_WARN_AFTER or picked_at(job):
        return ""
    return f"Job #{job.pk} ждёт worker'а {int(waited.total_seconds() // 60)} мин — worker не берёт задачу."


def dequeue_candidates(jobs: list[GenerationJob]) -> list[GenerationJob]:
    """PENDING jobs the operator may take out of the queue (decision C-2)."""
    return [job for job in jobs if queue_wait_text(job)]


def latest_finished(order, now=None) -> GenerationJob | None:
    """The job that finished most recently within LAST_RESULT_WINDOW."""
    now = now or timezone.now()
    finished = [
        job for job in order.generation_jobs.all()
        if job.finished_at and job.status in (GenerationJob.Status.SUCCEEDED, GenerationJob.Status.FAILED)
        and now - job.finished_at <= LAST_RESULT_WINDOW
    ]
    if not finished:
        return None
    return max(finished, key=lambda j: (j.finished_at, j.pk))


def duration_text(job: GenerationJob) -> str:
    worker = (job.output_metadata or {}).get(WORKER_KEY) or {}
    seconds = worker.get("duration_s")
    if seconds is None and job.started_at and job.finished_at:
        seconds = (job.finished_at - job.started_at).total_seconds()
    if seconds is None:
        return ""
    return f"{int(round(float(seconds)))} с"


def last_result_text(order, job: GenerationJob) -> str:
    """«Последняя генерация: …» — success / failure / ambiguous / dequeued."""
    title = job_title(order, job)
    duration = duration_text(job)
    took = f" за {duration}" if duration else ""
    if job.status == GenerationJob.Status.SUCCEEDED:
        asset_id = (job.output_metadata or {}).get("asset_id")
        what = f"превью #{asset_id}" if asset_id and job.task_type != GenerationJob.TaskType.FULL else title
        return f"Последняя генерация: {what} готово{took} (job #{job.pk})."
    failure_class = (job.output_metadata or {}).get("failure_class")
    if failure_class == "ambiguous":
        return (
            f"Последняя генерация: job #{job.pk} ({title}) — неоднозначно: worker не ответил за "
            f"{int(STALE_RUNNING_AFTER.total_seconds() // 60)} мин, возможно, вызов был оплачен. "
            "Не повторяйте вслепую — проверьте историю генераций."
        )
    if failure_class == QUEUE_LOST:
        return f"Последняя генерация: job #{job.pk} ({title}) снят из очереди — провайдер не вызывался, можно запускать заново."
    return f"Последняя генерация: job #{job.pk} ({title}) — ошибка: {job_error_text(job) or 'без описания'}"


# ----------------------------------------------------------- worker health


@dataclass(frozen=True)
class WorkerHealth:
    alive: bool
    text: str


def worker_health(now=None) -> WorkerHealth | None:
    """Heartbeat of the RQ worker on the generation queue; None when the
    background worker is not enabled (inline mode — nothing to show)."""
    if not worker_enabled():
        return None
    try:
        from rq import Worker

        from apps.core.services.generation_queue import rq_connection, rq_queue

        connection = rq_connection()
        workers = Worker.all(queue=rq_queue(connection))
    except Exception as exc:  # redis down, rq error
        return WorkerHealth(False, f"worker: нет связи с очередью ({type(exc).__name__})")
    now = now or datetime.now(dt_timezone.utc)
    youngest = None
    for worker in workers:
        beat = worker.last_heartbeat
        if beat is None:
            continue
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=dt_timezone.utc)
        age = now - beat
        if youngest is None or age < youngest:
            youngest = age
    if youngest is None:
        return WorkerHealth(False, "worker: не запущен (нет ни одного worker'а на очереди)")
    seconds = int(youngest.total_seconds())
    if youngest <= WORKER_HEARTBEAT_MAX_AGE:
        return WorkerHealth(True, f"worker: жив (heartbeat {seconds} с назад)")
    return WorkerHealth(False, f"worker: нет ответа {seconds // 60} мин {seconds % 60} с")
