import enum
import fnmatch
import json
import os
from collections.abc import Sequence
from datetime import timedelta
from typing import cast

import sentry_sdk
from opentelemetry import trace
from rq.job import Job

from config import (
    ENABLE_RESCAN_ON_FILESYSTEM_CHANGE,
    LIBRARY_BASE_PATH,
    RESCAN_ON_FILESYSTEM_CHANGE_DELAY,
    SCAN_TIMEOUT,
    SENTRY_DSN,
)
from config.config_manager import config_manager as cm
from endpoints.sockets.scan import STOP_SCAN_FLAG, scan_platforms
from handler.database import db_platform_handler
from handler.metadata import (
    meta_flashpoint_handler,
    meta_hasheous_handler,
    meta_hltb_handler,
    meta_igdb_handler,
    meta_launchbox_handler,
    meta_libretro_handler,
    meta_moby_handler,
    meta_playmatch_handler,
    meta_ra_handler,
    meta_sgdb_handler,
    meta_ss_handler,
    meta_tgdb_handler,
)
from handler.scan_handler import MetadataSource, ScanType
from logger.formatter import CYAN
from logger.formatter import highlight as hl
from logger.logger import log
from tasks.queue import enqueue_func, pending_jobs_for
from tasks.tasks import SCAN_JOB_GROUP, TaskType
from utils import get_version

sentry_sdk.init(
    dsn=SENTRY_DSN,
    release=f"romm@{get_version()}",
)
tracer = trace.get_tracer(__name__)


@enum.unique
class EventType(enum.StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


VALID_EVENTS = frozenset(
    (
        EventType.ADDED,
        EventType.DELETED,
    )
)

# A change is a tuple representing a file change, first element is the event type, second is the
# path of the file or directory that changed.
Change = tuple[EventType, str]


def scanned_platform_ids(job: Job) -> list[int] | None:
    """The platforms a pending scan covers, or None when it covers everything.

    Scans are enqueued with keyword arguments, so the platform list is in
    ``kwargs`` rather than ``args``.
    """
    platform_ids = job.kwargs.get("platform_ids") if job.kwargs else None
    return platform_ids or None


def _enqueue_scan(
    *,
    platform_ids: list[int],
    metadata_sources: list[str],
    scan_type: ScanType,
    task_name: str,
    delay: timedelta,
) -> None:
    """Queue a delayed scan for the platforms a change touched.

    Deliberately not single-run: several platforms can change at once, and each
    gets its own scan behind the others. The coverage check above is what keeps a
    platform from being scanned twice over.
    """
    enqueue_func(
        scan_platforms,
        task_name=task_name,
        task_type=TaskType.SCAN,
        timeout=SCAN_TIMEOUT,
        job_group=SCAN_JOB_GROUP,
        single_run=False,
        stop_flag=STOP_SCAN_FLAG,
        func_kwargs={
            "platform_ids": platform_ids,
            "metadata_sources": metadata_sources,
            "scan_type": scan_type,
        },
        delay=delay,
    )


def process_changes(changes: Sequence[Change]) -> None:
    if not ENABLE_RESCAN_ON_FILESYSTEM_CHANGE:
        return

    # Filter for valid events, applying the same exclusion rules as the scanner:
    # exact-match and fnmatch patterns for files, plus excluded directory names
    # checked against every path component so events inside excluded dirs are ignored.
    cnfg = cm.get_config()
    structure_level = 1 if cnfg.has_structure_path_b else 2
    excluded_patterns = (
        cnfg.EXCLUDED_SINGLE_FILES
        + cnfg.EXCLUDED_MULTI_FILES
        + cnfg.EXCLUDED_MULTI_PARTS_FILES
    )

    def _is_excluded(path: str) -> bool:
        parts = path.strip("/").split("/")
        for part in parts:
            if part.startswith(".romm_tmp_"):
                return True
            if any(
                part == pat or fnmatch.fnmatch(part, pat) for pat in excluded_patterns
            ):
                return True
        return False

    changes = [
        change
        for change in changes
        if change[0] in VALID_EVENTS
        and not _is_excluded(os.fsdecode(change[1]).split(LIBRARY_BASE_PATH)[-1])
    ]
    if not changes:
        return

    with tracer.start_as_current_span("process_changes"):
        # Find affected platform slugs
        fs_slugs: set[str] = set()
        changes_platform_directory = False
        for change in changes:
            event_type, change_path = change
            src_path = os.fsdecode(change_path)
            event_src = src_path.split(LIBRARY_BASE_PATH)[-1]
            event_src_parts = event_src.split("/")
            if len(event_src_parts) <= structure_level:
                log.warning(
                    f"Filesystem event path '{event_src}' does not have enough segments for structure_level {structure_level}. Skipping event."
                )
                continue

            if len(event_src_parts) == structure_level + 1:
                changes_platform_directory = True

            log.info(f"Filesystem event: {event_type} {event_src}")
            fs_slugs.add(event_src_parts[structure_level])

        if not fs_slugs:
            log.info("No valid filesystem slugs found in changes, exiting...")
            return

        # Check whether any metadata source is enabled
        source_mapping: dict[str, bool] = {
            MetadataSource.IGDB: meta_igdb_handler.is_enabled(),
            MetadataSource.SS: meta_ss_handler.is_enabled(),
            MetadataSource.MOBY: meta_moby_handler.is_enabled(),
            MetadataSource.RA: meta_ra_handler.is_enabled(),
            MetadataSource.LAUNCHBOX: meta_launchbox_handler.is_enabled(),
            MetadataSource.HASHEOUS: meta_hasheous_handler.is_enabled(),
            MetadataSource.PLAYMATCH: meta_playmatch_handler.is_enabled(),
            MetadataSource.SGDB: meta_sgdb_handler.is_enabled(),
            MetadataSource.FLASHPOINT: meta_flashpoint_handler.is_enabled(),
            MetadataSource.HLTB: meta_hltb_handler.is_enabled(),
            MetadataSource.TGDB: meta_tgdb_handler.is_enabled(),
            MetadataSource.LIBRETRO: meta_libretro_handler.is_enabled(),
        }
        metadata_sources = [source for source, flag in source_mapping.items() if flag]
        if not metadata_sources:
            log.warning("No metadata sources enabled, skipping rescan")
            return

        # Scans already running, queued, or waiting out their delay
        pending = pending_jobs_for(SCAN_JOB_GROUP)

        # A pending scan with no platform list covers the whole library, so
        # there is nothing left for this change to add
        if any(scanned_platform_ids(job) is None for job in pending):
            log.info("Full rescan already scheduled")
            return

        time_delta = timedelta(minutes=RESCAN_ON_FILESYSTEM_CHANGE_DELAY)
        rescan_in_msg = f"rescanning in {hl(str(RESCAN_ON_FILESYSTEM_CHANGE_DELAY), color=CYAN)} minutes."

        # Any change to a platform directory should trigger a full rescan
        if changes_platform_directory:
            log.info(f"Platform directory changed, {rescan_in_msg}")
            _enqueue_scan(
                platform_ids=[],
                metadata_sources=metadata_sources,
                scan_type=ScanType.UPDATE,
                task_name="Unidentified Scan",
                delay=time_delta,
            )
            return

        already_pending = {
            platform_id
            for job in pending
            for platform_id in scanned_platform_ids(job) or ()
        }

        # Otherwise, process each platform slug
        for fs_slug in fs_slugs:
            # TODO: Query platforms from the database in bulk
            db_platform = db_platform_handler.get_platform_by_fs_slug(fs_slug)
            if not db_platform:
                continue

            # Skip if a scan is already scheduled for this platform
            if db_platform.id in already_pending:
                log.info(f"Scan already scheduled for {hl(fs_slug)}")
                continue

            log.info(f"Change detected in {hl(fs_slug)} folder, {rescan_in_msg}")
            _enqueue_scan(
                platform_ids=[db_platform.id],
                metadata_sources=metadata_sources,
                scan_type=ScanType.QUICK,
                task_name="Quick Scan",
                delay=time_delta,
            )
            already_pending.add(db_platform.id)


if __name__ == "__main__":
    changes = cast(list[Change], json.loads(os.getenv("WATCHFILES_CHANGES", "[]")))
    if changes:
        process_changes(changes)
