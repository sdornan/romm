"""Queue policy for RQ jobs: duplicate runs, position, and cancellation.

Every enqueue funnels through here so "is a run of this already pending?" gets
the same answer everywhere. The answer comes from job meta rather than from
matching function names, which drift when code moves and cannot be read back at
all once the function is gone.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator
from datetime import timedelta
from itertools import chain
from typing import Any, Final

from rq import Queue, Worker
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation
from rq.job import Job, JobStatus

from config import TASK_RESULT_TTL
from exceptions.task_exceptions import TaskAlreadyQueuedException
from handler.redis_handler import (
    QueuePrio,
    default_queue,
    high_prio_queue,
    low_prio_queue,
    redis_client,
)
from logger.logger import log
from tasks.tasks import (
    META_CRON_STRING,
    META_JOB_GROUP,
    META_STOP_FLAG,
    Task,
    TaskType,
    build_job_meta,
    tasks_scheduler,
)

# The order the worker drains queues, matching the `rq worker high default low`
# arguments in docker/init_scripts/init.
QUEUE_ORDER: Final = (QueuePrio.HIGH, QueuePrio.DEFAULT, QueuePrio.LOW)

QUEUES: Final[dict[QueuePrio, Queue]] = {
    QueuePrio.HIGH: high_prio_queue,
    QueuePrio.DEFAULT: default_queue,
    QueuePrio.LOW: low_prio_queue,
}

DEBOUNCE_KEY_PREFIX: Final = "queue:debounce:"
DEBOUNCE_SECONDS: Final = 5


@enum.unique
class CancelOutcome(enum.StrEnum):
    """What cancelling a job actually did."""

    # Dropped before it ever ran.
    CANCELED = "canceled"
    # Running, and asked to unwind itself.
    STOPPING = "stopping"
    # Nothing to do: it already finished, failed, or was cancelled.
    ALREADY_DONE = "already_done"


def running_jobs() -> Iterator[Job]:
    """Jobs a worker currently holds.

    A started job has left its queue, so the workers are the only place it can
    still be found.
    """
    for worker in Worker.all(connection=redis_client):
        job = worker.get_current_job()
        if job is not None:
            yield job


def queued_jobs() -> Iterator[Job]:
    """Jobs waiting in any of the priority queues."""
    for prio in QUEUE_ORDER:
        yield from QUEUES[prio].get_jobs()


def delayed_jobs() -> Iterator[Job]:
    """One-off jobs waiting out a delay in the scheduler.

    Standing cron entries live in the same registry but represent a schedule
    rather than a run, so they are skipped.
    """
    for job in tasks_scheduler.get_jobs():
        if isinstance(job, Job) and not job.get_meta().get(META_CRON_STRING):
            yield job


def pending_jobs() -> Iterator[Job]:
    """Every job that is running, queued, or waiting out a delay."""
    seen: set[str] = set()
    for job in chain(running_jobs(), queued_jobs(), delayed_jobs()):
        if job.id not in seen:
            seen.add(job.id)
            yield job


def pending_jobs_for(job_group: str) -> list[Job]:
    """Pending jobs belonging to the given group."""
    return [
        job for job in pending_jobs() if job.get_meta().get(META_JOB_GROUP) == job_group
    ]


def running_job_for(job_group: str) -> Job | None:
    """The job in the given group that is on a worker right now."""
    for job in running_jobs():
        if job.get_meta().get(META_JOB_GROUP) == job_group:
            return job

    return None


def queue_position(job: Job) -> int | None:
    """How many jobs the worker has to get through before this one.

    RQ reports a position within a single queue, but the worker drains high,
    then default, then low, so a low-priority job also waits behind everything
    in the queues above it. Returns None for jobs that are not queued.
    """
    if job.get_status() != JobStatus.QUEUED:
        return None

    ahead = 0
    for prio in QUEUE_ORDER:
        queue = QUEUES[prio]
        if queue.name == job.origin:
            position = queue.get_job_position(job.id)
            return None if position is None else ahead + position
        ahead += queue.count

    return None


def _reject_if_pending(job_group: str | None) -> None:
    """Refuse a second run of a group that already has one in flight.

    Reading the queues and then writing to them is not atomic, so this alone
    cannot rule out two runs: nothing awaits between the two, which makes it
    atomic within one web process, but a second web process (or the watcher, or
    startup) can still slip through. `_claim_debounce` narrows that window; a
    lock wide enough to close it would have to outlive the job it guards, and a
    stale one would block scans for the four hours a scan is allowed to take.
    Two sequential scans cost duplicated work, which is the cheaper failure.
    """
    if not job_group:
        return

    conflicts = pending_jobs_for(job_group)
    if conflicts:
        raise TaskAlreadyQueuedException(job_group, conflicts[0].id)


def _claim_debounce(job_group: str | None) -> None:
    """Collapse submissions that arrive together into the first one.

    Covers what the pending check cannot see: a run enqueued microseconds ago,
    from another process, that no queue reports yet. The window is deliberately
    short. It is not released on completion, so a job that finishes inside the
    window would make a genuine resubmission look like a duplicate; seconds keep
    that invisible while still catching a double click or a second browser tab.
    """
    if not job_group:
        return

    claimed = redis_client.set(
        f"{DEBOUNCE_KEY_PREFIX}{job_group}", 1, nx=True, ex=DEBOUNCE_SECONDS
    )
    if not claimed:
        raise TaskAlreadyQueuedException(job_group)


def enqueue_func(
    func: Callable[..., Any],
    *,
    task_name: str,
    task_type: TaskType,
    timeout: int,
    queue_prio: QueuePrio = QueuePrio.LOW,
    job_group: str | None = None,
    single_run: bool = True,
    stop_flag: str | None = None,
    func_kwargs: dict[str, Any] | None = None,
    delay: timedelta | None = None,
    job_id: str | None = None,
    result_ttl: int = TASK_RESULT_TTL,
) -> Job:
    """Enqueue a plain function, refusing a second run of its group.

    Pass ``single_run=False`` to label the job with its group without enforcing
    one run at a time, for callers that do their own, narrower duplicate check
    and are allowed to queue behind another job in the group.

    Raises:
        TaskAlreadyQueuedException: the group already has a run in flight.
    """
    if single_run:
        _reject_if_pending(job_group)
        _claim_debounce(job_group)

    meta = build_job_meta(
        task_name=task_name,
        task_type=task_type,
        job_group=job_group,
        stop_flag=stop_flag,
    )
    kwargs = func_kwargs or {}

    if delay is not None:
        # Scheduler kwargs are named differently to the queue's, and anything
        # left over is passed through to the function.
        return tasks_scheduler.enqueue_in(
            delay,
            func,
            timeout=timeout,
            job_result_ttl=result_ttl,
            job_id=job_id,
            meta=meta,
            **kwargs,
        )

    return QUEUES[queue_prio].enqueue(
        func,
        kwargs=kwargs,
        job_timeout=timeout,
        result_ttl=result_ttl,
        job_id=job_id,
        meta=meta,
    )


def enqueue_task(
    task: Task,
    *,
    func_kwargs: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> Job:
    """Enqueue a task instance under the policy it declares.

    Raises:
        TaskAlreadyQueuedException: another run of the task is already pending.
    """
    return enqueue_func(
        task.run,
        task_name=task.title,
        task_type=task.task_type,
        timeout=task.timeout,
        queue_prio=task.queue_prio,
        job_group=task.job_group,
        stop_flag=task.stop_flag,
        func_kwargs=func_kwargs,
        job_id=job_id,
    )


def cancel_job(job: Job) -> CancelOutcome:
    """Cancel a job, asking it to unwind itself if it is already running.

    A running job cannot be interrupted from outside, so tasks that declare a
    stop flag get it set and are trusted to poll it. Anything else is killed
    outright, which loses whatever the job was part way through.
    """
    status = job.get_status()
    if status in (
        JobStatus.FINISHED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.STOPPED,
    ):
        return CancelOutcome.ALREADY_DONE

    if status == JobStatus.STARTED:
        # The job's terminal status is the worker's to write, not ours. Marking
        # it cancelled here only gets overwritten: RQ records success without
        # consulting whether the job was cancelled, so a scan that polls its stop
        # flag and unwinds cleanly would report itself finished moments later.
        stop_flag = job.get_meta().get(META_STOP_FLAG)
        if stop_flag:
            redis_client.set(stop_flag, 1)
        else:
            # Killing the work horse makes RQ record the job as stopped.
            send_stop_job_command(redis_client, job.id)

        return CancelOutcome.STOPPING

    _drop(job)
    return CancelOutcome.CANCELED


def _queue_holding(job: Job) -> Queue | None:
    """The queue a job was enqueued on, if it is one of ours."""
    return next((queue for queue in QUEUES.values() if queue.name == job.origin), None)


def _drop(job: Job) -> None:
    """Take a job out of circulation without disturbing its schedule.

    rq-scheduler reuses one job object for every run of a periodic task, keeping
    it in the scheduler registry while the current run is queued. Cancelling that
    object, or taking it out of the registry, would stop the task from ever
    running again, so a cron job is only pulled from the queue it is waiting in.
    """
    if job.get_meta().get(META_CRON_STRING):
        queue = _queue_holding(job)
        if queue is not None:
            queue.remove(job)
        return

    tasks_scheduler.cancel(job)
    try:
        job.cancel()
    except InvalidJobOperation as e:
        log.debug(f"Could not cancel job {job.id}: {e}")
