from abc import ABC, abstractmethod
from enum import Enum
from itertools import chain
from typing import Any, Final

import httpx
from rq import get_current_job
from rq.job import Job
from rq_scheduler import Scheduler

from config import TASK_TIMEOUT
from exceptions.task_exceptions import SchedulerException
from handler.redis_handler import QueuePrio, get_job_func_name, low_prio_queue
from logger.logger import log
from utils.context import ctx_httpx_client

tasks_scheduler = Scheduler(queue=low_prio_queue, connection=low_prio_queue.connection)

# Lives here rather than in the task module so scan job discovery can recognise
# the scheduled rescan without importing it, which would close an import cycle.
SCAN_LIBRARY_TASK_FUNC: Final = "tasks.scheduled.scan_library.scan_library_task.run"

# Held by every job that runs a scan, whoever started it: the socket handler,
# the filesystem watcher, or the scheduled rescan.
SCAN_JOB_GROUP: Final = "scan"

# Job meta keys. `task_name` and `task_type` feed the task status endpoint; the
# next two drive duplicate checks and cancellation in `tasks.queue`.
# `cron_string` is rq-scheduler's own, stamped on standing periodic entries.
META_TASK_NAME: Final = "task_name"
META_TASK_TYPE: Final = "task_type"
META_JOB_GROUP: Final = "job_group"
META_STOP_FLAG: Final = "stop_flag"
META_CRON_STRING: Final = "cron_string"

# Set by a task that unwound because it was asked to stop. RQ's own STOPPED
# status is not reachable from here: the worker sets it when it kills a work
# horse, while a task that stops cooperatively returns normally and is recorded
# as finished. Status describes how the callable exited; this describes what the
# task decided, so it belongs in meta.
META_STOPPED: Final = "stopped"


def update_job_meta(metadata: dict[str, Any]) -> None:
    """Update the current RQ job's meta data with update stats information"""
    try:
        current_job = get_current_job()
        if current_job:
            current_job.meta.update(metadata)
            current_job.save_meta()
    except Exception as e:
        # Silently fail if we can't update meta (e.g., not running in RQ context)
        log.debug(f"Could not update job meta: {e}")


class TaskType(str, Enum):
    """Enumeration of task types for categorization and UI display."""

    SCAN = "scan"
    CONVERSION = "conversion"
    CLEANUP = "cleanup"
    UPDATE = "update"
    SYNC = "sync"
    WATCHER = "watcher"
    GENERIC = "generic"


def build_job_meta(
    *,
    task_name: str,
    task_type: TaskType,
    job_group: str | None = None,
    stop_flag: str | None = None,
) -> dict[str, Any]:
    """The meta every enqueue attaches, so job discovery reads one shape."""
    meta: dict[str, Any] = {
        META_TASK_NAME: task_name,
        META_TASK_TYPE: task_type.value,
    }
    if job_group:
        meta[META_JOB_GROUP] = job_group
    if stop_flag:
        meta[META_STOP_FLAG] = stop_flag

    return meta


class Task(ABC):
    """Base class for all RQ tasks."""

    title: str
    description: str
    enabled: bool
    manual_run: bool
    cron_string: str | None = None
    task_type: TaskType
    timeout: int
    queue_prio: QueuePrio
    job_group: str | None = None
    stop_flag: str | None = None

    def __init__(
        self,
        title: str,
        description: str,
        task_type: TaskType,
        enabled: bool = False,
        manual_run: bool = False,
        cron_string: str | None = None,
        timeout: int = TASK_TIMEOUT,
        queue_prio: QueuePrio = QueuePrio.LOW,
        job_group: str | None = None,
        stop_flag: str | None = None,
    ):
        self.title = title
        self.description = description or title
        self.task_type = task_type
        self.enabled = enabled
        self.manual_run = manual_run
        self.cron_string = cron_string
        self.timeout = timeout
        self.queue_prio = queue_prio
        # Names the kind of work, so cancelling and duplicate checks can find
        # every job doing it whoever started it. Enqueues default to one run of a
        # group at a time; `tasks.queue.enqueue_func` can opt out per call. Tasks
        # leaving this None can be queued any number of times over.
        self.job_group = job_group
        # A redis key the task polls so it can unwind itself when asked to stop.
        self.stop_flag = stop_flag

    @property
    def can_run_manually(self) -> bool:
        """Whether an admin can trigger this task on demand."""
        return self.manual_run and self.enabled

    @abstractmethod
    async def run(self, *args: Any, **kwargs: Any) -> Any: ...


class PeriodicTask(Task, ABC):
    """Base class for periodic tasks that can be scheduled."""

    def __init__(self, *args: Any, func: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.func = func

    def _get_existing_job(self) -> Job | None:
        existing_jobs = chain(tasks_scheduler.get_jobs(), low_prio_queue.get_jobs())
        for job in existing_jobs:
            if isinstance(job, Job) and get_job_func_name(job) == self.func:
                return job

        return None

    def init(self) -> Job | None:
        """Initialize the task by scheduling or unscheduling it based on its state.

        Returns the scheduled job if it was successfully scheduled, or None if it was already
        scheduled or unscheduled.
        """
        job = self._get_existing_job()

        if self.enabled and not job:
            return self.schedule()
        elif job and not self.enabled:
            self.unschedule()
            return None
        return None

    def schedule(self) -> Job | None:
        """Schedule the task if it is enabled and not already scheduled.

        Returns the scheduled job if successful, or None otherwise.
        """
        if not self.enabled:
            raise SchedulerException(f"Scheduled {self.description} is not enabled.")

        if self._get_existing_job():
            log.info(f"{self.description.capitalize()} is already scheduled.")
            return None

        if self.cron_string:
            return tasks_scheduler.cron(
                self.cron_string,
                func=self.func,
                repeat=None,
                timeout=self.timeout,
                meta=build_job_meta(
                    task_name=self.title,
                    task_type=self.task_type,
                    job_group=self.job_group,
                    stop_flag=self.stop_flag,
                ),
            )

        return None

    def unschedule(self) -> bool:
        """Unschedule the task if it is currently scheduled.

        Returns whether the unscheduling was successful.
        """
        job = self._get_existing_job()
        if not job:
            log.info(f"{self.description.capitalize()} is not scheduled.")
            return False

        tasks_scheduler.cancel(job)
        log.info(f"{self.description.capitalize()} unscheduled.")
        return True


class RemoteFilePullTask(PeriodicTask, ABC):
    """Base class for tasks that pull files from a remote URL."""

    def __init__(self, *args: Any, url: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.url = url

    async def run(self, force: bool = False) -> Any:
        if not self.enabled and not force:
            log.info(f"Scheduled {self.description} not enabled, unscheduling...")
            self.unschedule()
            return None

        log.info(f"Scheduled {self.description} started...")

        httpx_client = ctx_httpx_client.get()
        try:
            response = await httpx_client.get(self.url, timeout=120)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as e:
            log.error(f"Scheduled {self.description} failed", exc_info=True)
            log.error(e)
            return None
