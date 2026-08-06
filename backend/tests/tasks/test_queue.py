from datetime import timedelta
from itertools import count
from unittest.mock import MagicMock

import pytest
from fakeredis import FakeRedis
from rq import Queue
from rq.job import Job, JobStatus

from exceptions.task_exceptions import TaskAlreadyQueuedException
from handler.redis_handler import QueuePrio
from tasks import queue as queue_module
from tasks.queue import (
    CancelOutcome,
    cancel_job,
    enqueue_func,
    enqueue_task,
    pending_jobs_for,
    queue_position,
    running_job_for,
)
from tasks.tasks import (
    META_CRON_STRING,
    META_JOB_GROUP,
    META_STOP_FLAG,
    Task,
    TaskType,
)

_job_ids = count()


@pytest.fixture(autouse=True)
def isolated_debounce(mocker):
    """Give each test its own debounce state rather than the shared instance's."""
    return mocker.patch.object(queue_module, "redis_client", FakeRedis())


def noop() -> None:
    """Target for enqueues that are never executed."""


def make_job(
    *,
    job_group: str | None = "scan",
    status=JobStatus.QUEUED,
    stop_flag: str | None = None,
    cron_string: str | None = None,
):
    job = MagicMock(spec=Job)
    job.id = f"job-{next(_job_ids)}"
    job.get_status.return_value = status
    job.origin = QueuePrio.LOW.value

    meta: dict[str, object] = {}
    if job_group:
        meta[META_JOB_GROUP] = job_group
    if stop_flag:
        meta[META_STOP_FLAG] = stop_flag
    if cron_string:
        meta[META_CRON_STRING] = cron_string
    job.get_meta.return_value = meta

    return job


@pytest.fixture
def jobs(mocker):
    """Control what each source of pending jobs reports."""

    def _patch(*, running=None, high=(), default=(), low=(), scheduled=()):
        worker = MagicMock()
        worker.get_current_job.return_value = running
        mocker.patch.object(queue_module.Worker, "all", return_value=[worker])

        for prio, queued in (
            (QueuePrio.HIGH, high),
            (QueuePrio.DEFAULT, default),
            (QueuePrio.LOW, low),
        ):
            mocker.patch.object(
                queue_module.QUEUES[prio], "get_jobs", return_value=list(queued)
            )

        mocker.patch.object(
            queue_module.tasks_scheduler, "get_jobs", return_value=list(scheduled)
        )

    return _patch


class DummyTask(Task):
    def __init__(self, **kwargs):
        kwargs.setdefault("title", "Dummy")
        kwargs.setdefault("description", "A dummy task")
        kwargs.setdefault("task_type", TaskType.CLEANUP)
        super().__init__(**kwargs)

    async def run(self, *args, **kwargs):
        return None


class TestPendingDiscovery:
    def test_finds_jobs_across_every_source(self, jobs):
        jobs(
            running=make_job(),
            high=[make_job()],
            low=[make_job()],
            scheduled=[make_job(status=JobStatus.SCHEDULED)],
        )

        assert len(pending_jobs_for("scan")) == 4

    def test_ignores_other_job_groups(self, jobs):
        jobs(running=make_job(job_group="cleanup"), high=[make_job()])

        assert len(pending_jobs_for("scan")) == 1

    def test_ignores_jobs_without_a_group(self, jobs):
        jobs(high=[make_job(job_group=None)])

        assert pending_jobs_for("scan") == []

    def test_skips_standing_cron_entries(self, jobs):
        # The entry stays in the scheduler for as long as the task is enabled,
        # so counting it would block the task from ever being queued.
        jobs(scheduled=[make_job(cron_string="0 3 * * *")])

        assert pending_jobs_for("scan") == []

    def test_counts_a_cron_job_once_it_is_queued(self, jobs):
        # rq-scheduler reuses one job for every run, putting it back in the
        # scheduler as soon as the current run is queued.
        job = make_job(cron_string="0 3 * * *")
        jobs(high=[job], scheduled=[job])

        assert [j.id for j in pending_jobs_for("scan")] == [job.id]

    def test_running_job_for_only_looks_at_workers(self, jobs):
        jobs(high=[make_job()])
        assert running_job_for("scan") is None

        jobs(running=make_job(status=JobStatus.STARTED))
        assert running_job_for("scan") is not None


class TestSingleRun:
    def test_refuses_a_second_run_of_the_group(self, jobs):
        jobs(running=make_job(status=JobStatus.STARTED))

        with pytest.raises(TaskAlreadyQueuedException) as excinfo:
            enqueue_func(
                noop,
                task_name="Scan",
                task_type=TaskType.SCAN,
                timeout=60,
                job_group="scan",
            )

        assert excinfo.value.job_group == "scan"

    def test_reports_the_job_that_blocked_it(self, jobs):
        blocking = make_job(status=JobStatus.STARTED)
        jobs(running=blocking)

        with pytest.raises(TaskAlreadyQueuedException) as excinfo:
            enqueue_func(
                noop,
                task_name="Scan",
                task_type=TaskType.SCAN,
                timeout=60,
                job_group="scan",
            )

        assert excinfo.value.job_id == blocking.id

    def test_opting_out_enqueues_behind_the_holder(self, jobs, mocker):
        jobs(running=make_job(status=JobStatus.STARTED))
        enqueue = mocker.patch.object(queue_module.QUEUES[QueuePrio.LOW], "enqueue")

        enqueue_func(
            noop,
            task_name="Scan",
            task_type=TaskType.SCAN,
            timeout=60,
            job_group="scan",
            single_run=False,
        )

        enqueue.assert_called_once()

    def test_groupless_tasks_stack_up(self, jobs, mocker):
        jobs(running=make_job(job_group=None, status=JobStatus.STARTED))
        enqueue = mocker.patch.object(queue_module.QUEUES[QueuePrio.LOW], "enqueue")

        enqueue_func(noop, task_name="Any", task_type=TaskType.GENERIC, timeout=60)

        enqueue.assert_called_once()

    def test_task_declares_its_own_policy(self, jobs, mocker):
        jobs()
        enqueue = mocker.patch.object(queue_module.QUEUES[QueuePrio.HIGH], "enqueue")
        task = DummyTask(
            queue_prio=QueuePrio.HIGH,
            job_group="dummy",
            stop_flag="dummy:stop",
        )

        enqueue_task(task)

        meta = enqueue.call_args.kwargs["meta"]
        assert meta[META_JOB_GROUP] == "dummy"
        assert meta[META_STOP_FLAG] == "dummy:stop"


class TestDelayedEnqueue:
    def test_delay_goes_through_the_scheduler(self, jobs, mocker):
        jobs()
        enqueue_in = mocker.patch.object(queue_module.tasks_scheduler, "enqueue_in")

        enqueue_func(
            noop,
            task_name="Quick Scan",
            task_type=TaskType.SCAN,
            timeout=60,
            job_group="scan",
            func_kwargs={"platform_ids": [3]},
            delay=timedelta(minutes=5),
        )

        # Function kwargs ride alongside the scheduler's own, which is how
        # rq-scheduler passes them through to the job.
        assert enqueue_in.call_args.kwargs["platform_ids"] == [3]
        assert enqueue_in.call_args.args[0] == timedelta(minutes=5)


class TestQueuePosition:
    @pytest.fixture
    def queues(self, mocker):
        """Real queues on a fake redis, so positions are actually computed."""
        connection = FakeRedis()
        queues = {
            prio: Queue(name=prio.value, connection=connection)
            for prio in (QueuePrio.HIGH, QueuePrio.DEFAULT, QueuePrio.LOW)
        }
        mocker.patch.dict(queue_module.QUEUES, queues, clear=True)
        return queues

    def test_first_in_line_is_zero(self, queues):
        job = queues[QueuePrio.HIGH].enqueue(noop)

        assert queue_position(job) == 0

    def test_counts_jobs_ahead_in_the_same_queue(self, queues):
        queues[QueuePrio.LOW].enqueue(noop)
        job = queues[QueuePrio.LOW].enqueue(noop)

        assert queue_position(job) == 1

    def test_counts_higher_priority_queues(self, queues):
        # The worker drains high, then default, then low, so a low priority job
        # waits behind every job in the queues above it.
        queues[QueuePrio.HIGH].enqueue(noop)
        queues[QueuePrio.HIGH].enqueue(noop)
        queues[QueuePrio.DEFAULT].enqueue(noop)
        job = queues[QueuePrio.LOW].enqueue(noop)

        assert queue_position(job) == 3

    def test_none_once_the_job_is_no_longer_waiting(self, queues):
        job = queues[QueuePrio.HIGH].enqueue(noop)
        job.set_status(JobStatus.STARTED)

        assert queue_position(job) is None


class TestCancel:
    @pytest.fixture(autouse=True)
    def scheduler_cancel(self, mocker):
        return mocker.patch.object(queue_module.tasks_scheduler, "cancel")

    @pytest.fixture
    def redis(self, mocker):
        return mocker.patch.object(queue_module, "redis_client")

    def test_queued_job_is_dropped(self, redis, scheduler_cancel):
        job = make_job(status=JobStatus.QUEUED)

        assert cancel_job(job) == CancelOutcome.CANCELED
        job.cancel.assert_called_once()

    def test_delayed_job_leaves_the_scheduler(self, redis, scheduler_cancel):
        job = make_job(status=JobStatus.SCHEDULED)

        cancel_job(job)

        scheduler_cancel.assert_called_once_with(job)

    def test_running_job_gets_its_stop_flag_set(self, redis, scheduler_cancel):
        job = make_job(status=JobStatus.STARTED, stop_flag="scan:stop")

        assert cancel_job(job) == CancelOutcome.STOPPING
        redis.set.assert_called_once_with("scan:stop", 1)

    def test_running_jobs_status_is_left_to_the_worker(self, redis, scheduler_cancel):
        # RQ records success without checking whether a job was cancelled, so a
        # status set here is overwritten the moment the job unwinds: the UI would
        # show cancelled, then finished, for a job the user stopped.
        job = make_job(status=JobStatus.STARTED, stop_flag="scan:stop")

        cancel_job(job)

        job.cancel.assert_not_called()
        scheduler_cancel.assert_not_called()

    def test_running_job_without_a_stop_flag_is_killed(
        self, mocker, redis, scheduler_cancel
    ):
        send_stop = mocker.patch.object(queue_module, "send_stop_job_command")
        job = make_job(status=JobStatus.STARTED)

        assert cancel_job(job) == CancelOutcome.STOPPING
        send_stop.assert_called_once_with(redis, job.id)
        redis.set.assert_not_called()

    @pytest.mark.parametrize(
        "status",
        [
            JobStatus.FINISHED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
            JobStatus.STOPPED,
        ],
    )
    def test_finished_job_is_left_alone(self, redis, scheduler_cancel, status):
        job = make_job(status=status)

        assert cancel_job(job) == CancelOutcome.ALREADY_DONE
        job.cancel.assert_not_called()

    def test_cron_job_keeps_its_schedule(self, mocker, redis, scheduler_cancel):
        # Cancelling the shared job, or dropping it from the scheduler, would
        # stop the periodic task from ever running again.
        remove = mocker.patch.object(queue_module.QUEUES[QueuePrio.LOW], "remove")
        job = make_job(status=JobStatus.QUEUED, cron_string="0 3 * * *")

        assert cancel_job(job) == CancelOutcome.CANCELED
        job.cancel.assert_not_called()
        scheduler_cancel.assert_not_called()
        remove.assert_called_once_with(job)


class TestDebounce:
    """Submissions arriving together collapse into the first one.

    The pending check reads the queues, then the enqueue writes to them, and
    nothing awaits in between. That makes it atomic within one web process, but
    a second process can still slip through with a run too new for any queue to
    report. The debounce is what narrows that window.
    """

    @pytest.fixture(autouse=True)
    def nothing_pending(self, jobs, mocker):
        jobs()
        return mocker.patch.object(queue_module.QUEUES[QueuePrio.LOW], "enqueue")

    def _enqueue(self, job_group="scan"):
        return enqueue_func(
            noop,
            task_name="Scan",
            task_type=TaskType.SCAN,
            timeout=60,
            job_group=job_group,
        )

    def test_second_submission_in_the_window_is_refused(self):
        self._enqueue()

        with pytest.raises(TaskAlreadyQueuedException):
            self._enqueue()

    def test_refusal_names_no_job(self):
        # Whichever submission won the race may not have been enqueued yet, so
        # there is no id to hand back for the caller to follow.
        self._enqueue()

        with pytest.raises(TaskAlreadyQueuedException) as excinfo:
            self._enqueue()

        assert excinfo.value.job_id is None

    def test_another_group_is_unaffected(self):
        self._enqueue("scan")

        self._enqueue("cleanup_missing_roms")

    def test_a_group_opting_out_is_not_debounced(self, nothing_pending):
        for _ in range(3):
            enqueue_func(
                noop,
                task_name="Quick Scan",
                task_type=TaskType.SCAN,
                timeout=60,
                job_group="scan",
                single_run=False,
            )

        assert nothing_pending.call_count == 3


class TestAgainstRealQueues:
    """Discovery over real queues rather than mocked job sources.

    Every other test here stubs out what the queues report, which proves the
    checks are wired but not that they see an actual enqueue.
    """

    @pytest.fixture(autouse=True)
    def queues(self, mocker):
        connection = FakeRedis()
        mocker.patch.dict(
            queue_module.QUEUES,
            {
                prio: Queue(name=prio.value, connection=connection)
                for prio in (QueuePrio.HIGH, QueuePrio.DEFAULT, QueuePrio.LOW)
            },
            clear=True,
        )
        # No worker is running, and nothing is waiting out a delay; both still
        # reach for the shared instance otherwise.
        mocker.patch.object(queue_module.Worker, "all", return_value=[])
        mocker.patch.object(queue_module.tasks_scheduler, "get_jobs", return_value=[])

    def _enqueue(self):
        return enqueue_func(
            noop,
            task_name="Scan",
            task_type=TaskType.SCAN,
            timeout=60,
            queue_prio=QueuePrio.HIGH,
            job_group="scan",
        )

    def test_an_enqueued_job_is_discoverable_by_its_group(self):
        job = self._enqueue()

        assert [j.id for j in pending_jobs_for("scan")] == [job.id]

    def test_a_second_run_of_the_group_is_refused(self):
        self._enqueue()

        with pytest.raises(TaskAlreadyQueuedException):
            self._enqueue()

    def test_only_one_job_makes_it_onto_the_queue(self):
        self._enqueue()
        with pytest.raises(TaskAlreadyQueuedException):
            self._enqueue()

        assert queue_module.QUEUES[QueuePrio.HIGH].count == 1
