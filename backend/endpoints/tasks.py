from datetime import datetime, timezone
from typing import Any, TypedDict

from fastapi import Body, HTTPException, Request
from rq import Worker
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from rq.registry import FailedJobRegistry, FinishedJobRegistry

from config import (
    ENABLE_RESCAN_ON_FILESYSTEM_CHANGE,
    RESCAN_ON_FILESYSTEM_CHANGE_DELAY,
)
from decorators.auth import protected_route
from endpoints.responses import (
    CleanupTaskStatusResponse,
    ConversionTaskStatusResponse,
    GenericTaskStatusResponse,
    ScanTaskStatusResponse,
    SyncTaskStatusResponse,
    TaskCancelResponse,
    TaskExecutionResponse,
    TaskStatusResponse,
    UpdateTaskStatusResponse,
    WatcherTaskStatusResponse,
)
from endpoints.responses.tasks import GroupedTasksDict, TaskInfo
from exceptions.task_exceptions import TaskAlreadyQueuedException
from handler.auth.constants import Scope
from handler.redis_handler import (
    default_queue,
    get_job_func_name,
    high_prio_queue,
    low_prio_queue,
    redis_client,
)
from logger.logger import log
from tasks.manual.cleanup_missing_roms import cleanup_missing_roms_task
from tasks.manual.recompute_save_content_hashes import (
    recompute_save_content_hashes_task,
)
from tasks.manual.sync_folder_scan import sync_folder_scan_task
from tasks.queue import cancel_job, enqueue_task, queue_position
from tasks.scheduled.cleanup_orphaned_resources import cleanup_orphaned_resources_task
from tasks.scheduled.cleanup_zip_cache import cleanup_zip_cache_task
from tasks.scheduled.convert_images_to_webp import convert_images_to_webp_task
from tasks.scheduled.scan_library import scan_library_task
from tasks.scheduled.update_launchbox_metadata import update_launchbox_metadata_task
from tasks.scheduled.update_switch_titledb import update_switch_titledb_task
from tasks.tasks import (
    META_STOPPED,
    Task,
    TaskType,
)
from utils.router import APIRouter

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
)


class ScheduledTask(TypedDict):
    name: str
    type: TaskType
    task: Task


class ManualTask(ScheduledTask):
    pass


scheduled_tasks: list[ScheduledTask] = [
    ScheduledTask(
        {
            "name": "scan_library",
            "type": TaskType.SCAN,
            "task": scan_library_task,
        }
    ),
    ScheduledTask(
        {
            "name": "update_launchbox_metadata",
            "type": TaskType.UPDATE,
            "task": update_launchbox_metadata_task,
        }
    ),
    ScheduledTask(
        {
            "name": "update_switch_titledb",
            "type": TaskType.UPDATE,
            "task": update_switch_titledb_task,
        }
    ),
    ScheduledTask(
        {
            "name": "convert_images_to_webp",
            "type": TaskType.CONVERSION,
            "task": convert_images_to_webp_task,
        }
    ),
    ScheduledTask(
        {
            "name": "cleanup_zip_cache",
            "type": TaskType.CLEANUP,
            "task": cleanup_zip_cache_task,
        }
    ),
    ScheduledTask(
        {
            "name": "cleanup_orphaned_resources",
            "type": TaskType.CLEANUP,
            "task": cleanup_orphaned_resources_task,
        }
    ),
]

manual_tasks: list[ManualTask] = [
    ManualTask(
        {
            "name": "cleanup_missing_roms",
            "type": TaskType.CLEANUP,
            "task": cleanup_missing_roms_task,
        }
    ),
    ManualTask(
        {
            "name": "sync_folder_scan",
            "type": TaskType.SYNC,
            "task": sync_folder_scan_task,
        }
    ),
    ManualTask(
        {
            "name": "recompute_save_content_hashes",
            "type": TaskType.CLEANUP,
            "task": recompute_save_content_hashes_task,
        }
    ),
]


def _build_task_info(name: str, task: Task) -> TaskInfo:
    """Builds a TaskInfo object from task details."""
    return TaskInfo(
        name=name,
        type=task.task_type,
        title=task.title,
        description=task.description,
        enabled=task.enabled,
        manual_run=task.can_run_manually,
        cron_string=task.cron_string or "",
    )


def _reported_status(job: Job, job_meta: dict[str, Any]) -> JobStatus | None:
    """The job's status, corrected for tasks that unwound on request.

    A task asked to stop polls its stop flag and returns normally, so RQ records
    it as finished. Only the task itself knows it did not run to completion, and
    it says so in its meta.
    """
    status = job.get_status()
    if status == JobStatus.FINISHED and job_meta.get(META_STOPPED):
        return JobStatus.STOPPED

    return status


def _build_task_status_response(
    job: Job,
) -> TaskStatusResponse:
    job_meta = job.get_meta()
    task_type = job_meta.get("task_type")
    task_name = job_meta.get("task_name") or get_job_func_name(job)

    # Convert datetime objects to ISO format strings
    created_at = job.created_at.isoformat() if job.created_at else None
    started_at = job.started_at.isoformat() if job.started_at else None
    ended_at = job.ended_at.isoformat() if job.ended_at else None
    enqueued_at = job.enqueued_at.isoformat() if job.enqueued_at else None

    common_data = {
        "task_name": task_name,
        "task_id": job.id,
        "status": _reported_status(job, job_meta),
        "created_at": created_at,
        "enqueued_at": enqueued_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "queue_position": queue_position(job),
    }

    if not task_type:
        return GenericTaskStatusResponse(
            task_type=TaskType.GENERIC,
            meta={},
            **common_data,  # trunk-ignore(mypy/typeddict-item)
        )

    match TaskType(task_type):
        case TaskType.SCAN:
            return ScanTaskStatusResponse(
                task_type=TaskType.SCAN,
                meta={"scan_stats": job_meta.get("scan_stats")},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.CONVERSION:
            return ConversionTaskStatusResponse(
                task_type=TaskType.CONVERSION,
                meta={"conversion_stats": job_meta.get("conversion_stats")},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.UPDATE:
            return UpdateTaskStatusResponse(
                task_type=TaskType.UPDATE,
                meta={"update_stats": job_meta.get("update_stats")},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.CLEANUP:
            return CleanupTaskStatusResponse(
                task_type=TaskType.CLEANUP,
                meta={"cleanup_stats": job_meta.get("cleanup_stats")},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.SYNC:
            return SyncTaskStatusResponse(
                task_type=TaskType.SYNC,
                meta={},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.WATCHER:
            return WatcherTaskStatusResponse(
                task_type=TaskType.WATCHER,
                meta={},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case TaskType.GENERIC:
            return GenericTaskStatusResponse(
                task_type=TaskType.GENERIC,
                meta={},
                **common_data,  # trunk-ignore(mypy/typeddict-item)
            )
        case _:
            raise ValueError(f"Invalid task type: {task_type}")


@protected_route(router.get, "", [Scope.TASKS_RUN])
async def list_tasks(request: Request) -> GroupedTasksDict:
    """List all available tasks grouped by task type.

    Args:
        request (Request): FastAPI Request object
    Returns:
        GroupedTasksDict: Dictionary with tasks grouped by their type (scheduled, manual, watcher)
    """
    # Initialize the grouped tasks dictionary
    grouped_tasks: GroupedTasksDict = {
        "scheduled": [],
        "manual": [],
        "watcher": [],
    }

    for task in manual_tasks:
        grouped_tasks["manual"].append(_build_task_info(task["name"], task["task"]))

    for task in scheduled_tasks:
        grouped_tasks["scheduled"].append(_build_task_info(task["name"], task["task"]))

    # Add the adhoc watcher task
    grouped_tasks["watcher"].append(
        TaskInfo(
            name="filesystem_watcher",
            type=TaskType.WATCHER,
            title="Rescan on filesystem change",
            description=f"Runs a scan when a change is detected in the library path, with a {RESCAN_ON_FILESYSTEM_CHANGE_DELAY} minute delay",
            enabled=ENABLE_RESCAN_ON_FILESYSTEM_CHANGE,
            manual_run=False,
            cron_string="",
        )
    )

    return grouped_tasks


@protected_route(router.get, "/status", [Scope.TASKS_RUN])
async def get_tasks_status(request: Request) -> list[TaskStatusResponse]:
    """Get all active, queued, completed, and failed tasks.

    Args:
        request (Request): FastAPI Request object
    Returns:
        list[TaskStatusResponse]: List of all tasks with their current status
    """
    all_tasks: list[TaskStatusResponse] = []

    # Get currently running jobs from workers
    workers = Worker.all(connection=redis_client)
    for worker in workers:
        current_job = worker.get_current_job()
        if current_job:
            all_tasks.append(_build_task_status_response(current_job))

    # Get all jobs from the queues (including completed ones)
    low_prio_jobs = low_prio_queue.get_jobs()
    default_prio_jobs = default_queue.get_jobs()
    high_prio_jobs = high_prio_queue.get_jobs()

    for job in low_prio_jobs + default_prio_jobs + high_prio_jobs:
        all_tasks.append(_build_task_status_response(job))

    # Get finished jobs from all queues
    finished_registries = [
        FinishedJobRegistry(queue=low_prio_queue),
        FinishedJobRegistry(queue=default_queue),
        FinishedJobRegistry(queue=high_prio_queue),
    ]

    failed_registries = [
        FailedJobRegistry(queue=low_prio_queue),
        FailedJobRegistry(queue=default_queue),
        FailedJobRegistry(queue=high_prio_queue),
    ]

    # Process finished jobs
    for registry in finished_registries:
        for job_id in registry.get_job_ids():
            try:
                job = Job.fetch(job_id, connection=redis_client)
            except NoSuchJobError:
                registry.remove(job_id)
                continue
            all_tasks.append(
                _build_task_status_response(
                    job,
                )
            )

    # Process failed jobs
    for registry in failed_registries:
        for job_id in registry.get_job_ids():
            try:
                job = Job.fetch(job_id, connection=redis_client)
            except NoSuchJobError:
                registry.remove(job_id)
                continue
            all_tasks.append(_build_task_status_response(job))

    all_tasks.sort(
        key=lambda x: x["started_at"] or x["enqueued_at"] or x["created_at"] or "",
        reverse=True,
    )

    return all_tasks


@protected_route(router.get, "/{task_id}", [Scope.TASKS_RUN])
async def get_task_by_id(request: Request, task_id: str) -> TaskStatusResponse:
    """Get the status of a task by its job ID.

    Args:
        request (Request): FastAPI Request object
        task_id (str): Job ID of the task to retrieve status for
    Returns:
        TaskStatusResponse: Task status information
    """
    try:
        job = Job.fetch(task_id, connection=low_prio_queue.connection)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Task with ID '{task_id}' not found",
        ) from e

    return _build_task_status_response(job)


TASK_KWARGS = Body(default=None)


@protected_route(router.post, "/run/{task_name}", [Scope.TASKS_RUN])
async def run_single_task(
    request: Request,
    task_name: str,
    task_kwargs: dict[str, Any] | None = TASK_KWARGS,
) -> TaskExecutionResponse:
    """Run a single task endpoint.

    Args:
        request (Request): FastAPI Request object
        task_name (str): Name of the task to run
        task_kwargs (dict | None): Optional keyword arguments forwarded to the task's run() method
    Returns:
        TaskExecutionResponse: Task execution response with details
    """
    all_tasks = {task["name"]: task["task"] for task in manual_tasks + scheduled_tasks}

    if task_name not in all_tasks:
        available_tasks = list(all_tasks.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_name}' not found, available tasks are {', '.join(available_tasks)}",
        )

    task_instance = all_tasks[task_name]
    if not task_instance.can_run_manually:
        raise HTTPException(
            status_code=400,
            detail=f"Task '{task_name}' cannot be run",
        )

    try:
        job = enqueue_task(task_instance, func_kwargs=task_kwargs or {})
    except TaskAlreadyQueuedException as e:
        raise HTTPException(
            status_code=409,
            detail=f"Task '{task_name}' is already queued or running",
            # Lets the caller follow the run that is already going. Absent when
            # the debounce rejected the submission, which has no job to name.
            headers={"X-Existing-Task-Id": e.job_id} if e.job_id else None,
        ) from e

    return {
        "task_name": task_instance.title,
        "task_id": job.id,
        "status": job.get_status() or JobStatus.QUEUED,
        "created_at": (
            job.created_at.isoformat()
            if job.created_at
            else datetime.now(timezone.utc).isoformat()
        ),
        "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
        "queue_position": queue_position(job),
    }


@protected_route(router.delete, "/{task_id}", [Scope.TASKS_RUN])
async def cancel_task(request: Request, task_id: str) -> TaskCancelResponse:
    """Cancel a queued task, or ask a running one to stop.

    Args:
        request (Request): FastAPI Request object
        task_id (str): Job ID of the task to cancel
    Returns:
        TaskCancelResponse: What cancelling the task did
    """
    try:
        job = Job.fetch(task_id, connection=redis_client)
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Task with ID '{task_id}' not found",
        ) from e

    outcome = cancel_job(job)
    log.info(f"Task {task_id} cancellation requested: {outcome.value}")

    return {
        "task_id": task_id,
        "task_name": job.get_meta().get("task_name") or get_job_func_name(job),
        "outcome": outcome,
    }
