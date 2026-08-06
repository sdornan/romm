from unittest.mock import MagicMock

import pytest
import watcher as watcher_module
from rq.job import Job, JobStatus
from watcher import EventType, process_changes, scanned_platform_ids


def make_scan_job(platform_ids=None, *, status=JobStatus.SCHEDULED):
    """A pending scan job as the scheduler and queues report it.

    Scans are enqueued with keyword arguments, so the platform list lives in
    ``kwargs``; ``args`` is always empty.
    """
    job = MagicMock(spec=Job)
    job.id = "job-1"
    job.args = ()
    job.kwargs = {"platform_ids": platform_ids if platform_ids is not None else []}
    job.get_status.return_value = status
    return job


class TestScannedPlatformIds:
    def test_reads_the_platform_list_from_kwargs(self):
        assert scanned_platform_ids(make_scan_job([7, 9])) == [7, 9]

    def test_an_empty_list_means_the_whole_library(self):
        assert scanned_platform_ids(make_scan_job([])) is None

    def test_missing_kwargs_mean_the_whole_library(self):
        job = make_scan_job()
        job.kwargs = {}
        assert scanned_platform_ids(job) is None


class TestProcessChangesDedupe:
    """A change must not queue a scan the pending ones already cover."""

    @pytest.fixture
    def enqueue(self, mocker):
        mocker.patch.object(watcher_module, "ENABLE_RESCAN_ON_FILESYSTEM_CHANGE", True)
        mocker.patch.object(
            watcher_module, "meta_igdb_handler", MagicMock(is_enabled=lambda: True)
        )
        return mocker.patch.object(watcher_module, "_enqueue_scan")

    @pytest.fixture
    def pending(self, mocker):
        def _pending(jobs):
            mocker.patch.object(
                watcher_module, "pending_jobs_for", return_value=list(jobs)
            )

        _pending([])
        return _pending

    @pytest.fixture
    def platform(self, mocker):
        platform = MagicMock(id=7)
        mocker.patch.object(
            watcher_module.db_platform_handler,
            "get_platform_by_fs_slug",
            return_value=platform,
        )
        return platform

    def _change(self, path="/romm/library/roms/gba/game.gba"):
        return [(EventType.ADDED, path)]

    def test_queues_a_scan_for_a_changed_platform(self, enqueue, pending, platform):
        process_changes(self._change())

        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs["platform_ids"] == [7]

    def test_skips_a_platform_a_pending_scan_covers(self, enqueue, pending, platform):
        pending([make_scan_job([7])])

        process_changes(self._change())

        enqueue.assert_not_called()

    def test_still_queues_a_platform_no_pending_scan_covers(
        self, enqueue, pending, platform
    ):
        pending([make_scan_job([42])])

        process_changes(self._change())

        enqueue.assert_called_once()

    def test_skips_everything_when_a_full_rescan_is_pending(
        self, enqueue, pending, platform
    ):
        pending([make_scan_job([])])

        process_changes(self._change())

        enqueue.assert_not_called()
