import os

import pytest

from utils import hardware
from utils.hardware import (
    MAX_WORKERS,
    SIBLING_MEMORY_BYTES,
    WORKER_MEMORY_BYTES,
    detect_cpu_count,
    detect_memory_bytes,
    suggest_concurrency,
)

GIB = 1024 * 1024 * 1024


def _fake_files(monkeypatch, files: dict[str, str]) -> None:
    """Serve only the given cgroup/proc paths, so the rest read as absent."""
    monkeypatch.setattr(hardware, "_read_text", lambda path: files.get(path))


class TestCpuDetection:
    def test_uses_process_cpu_count_when_no_quota(self, monkeypatch):
        _fake_files(monkeypatch, {})
        monkeypatch.setattr(os, "process_cpu_count", lambda: 6)

        assert detect_cpu_count() == 6

    def test_cgroup_v2_quota_caps_the_count(self, monkeypatch):
        _fake_files(monkeypatch, {"/sys/fs/cgroup/cpu.max": "200000 100000"})
        monkeypatch.setattr(os, "process_cpu_count", lambda: 32)

        assert detect_cpu_count() == 2

    def test_cgroup_v2_unlimited_quota_is_ignored(self, monkeypatch):
        _fake_files(monkeypatch, {"/sys/fs/cgroup/cpu.max": "max 100000"})
        monkeypatch.setattr(os, "process_cpu_count", lambda: 4)

        assert detect_cpu_count() == 4

    def test_cgroup_v1_quota_caps_the_count(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "150000",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
            },
        )
        monkeypatch.setattr(os, "process_cpu_count", lambda: 8)

        # A 1.5-core allowance rounds up rather than down to a single worker.
        assert detect_cpu_count() == 2

    def test_cgroup_v1_unlimited_quota_is_ignored(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
            },
        )
        monkeypatch.setattr(os, "process_cpu_count", lambda: 4)

        assert detect_cpu_count() == 4

    def test_v2_presence_stops_the_v1_fallback(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {
                "/sys/fs/cgroup/cpu.max": "max 100000",
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "100000",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
            },
        )
        monkeypatch.setattr(os, "process_cpu_count", lambda: 4)

        assert detect_cpu_count() == 4

    def test_quota_below_one_core_still_yields_one(self, monkeypatch):
        _fake_files(monkeypatch, {"/sys/fs/cgroup/cpu.max": "20000 100000"})
        monkeypatch.setattr(os, "process_cpu_count", lambda: 4)

        assert detect_cpu_count() == 1

    def test_unreadable_counts_fall_back_to_one(self, monkeypatch):
        _fake_files(monkeypatch, {})
        monkeypatch.setattr(os, "process_cpu_count", lambda: None)
        monkeypatch.setattr(os, "cpu_count", lambda: None)

        assert detect_cpu_count() == 1


class TestMemoryDetection:
    def test_reads_cgroup_v2_limit(self, monkeypatch):
        _fake_files(monkeypatch, {"/sys/fs/cgroup/memory.max": str(2 * GIB)})

        assert detect_memory_bytes() == 2 * GIB

    def test_reads_cgroup_v1_limit(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {"/sys/fs/cgroup/memory/memory.limit_in_bytes": str(3 * GIB)},
        )

        assert detect_memory_bytes() == 3 * GIB

    def test_cgroup_v1_unlimited_sentinel_falls_back_to_meminfo(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {
                "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712",
                "/proc/meminfo": "MemTotal:       16461028 kB\nMemFree: 100 kB",
            },
        )

        assert detect_memory_bytes() == 16461028 * 1024

    def test_takes_the_lower_of_cgroup_and_physical(self, monkeypatch):
        _fake_files(
            monkeypatch,
            {
                "/sys/fs/cgroup/memory.max": str(64 * GIB),
                "/proc/meminfo": "MemTotal:        8388608 kB",
            },
        )

        # A cgroup limit above physical RAM is not a budget the host can honor.
        assert detect_memory_bytes() == 8 * GIB

    def test_returns_none_when_nothing_reports(self, monkeypatch):
        _fake_files(monkeypatch, {})

        assert detect_memory_bytes() is None

    def test_malformed_meminfo_returns_none(self, monkeypatch):
        _fake_files(monkeypatch, {"/proc/meminfo": "MemTotal:"})

        assert detect_memory_bytes() is None


class TestSuggestConcurrency:
    def test_follows_two_per_cpu_plus_one_when_memory_is_ample(self):
        assert suggest_concurrency(cpu_count=2, memory_bytes=64 * GIB) == 5

    def test_memory_caps_the_cpu_target(self):
        # Two workers' worth of spendable memory above the sibling reserve.
        memory = SIBLING_MEMORY_BYTES + 2 * WORKER_MEMORY_BYTES
        assert suggest_concurrency(cpu_count=8, memory_bytes=memory) == 2

    def test_never_exceeds_the_connection_pool_cap(self):
        assert suggest_concurrency(cpu_count=64, memory_bytes=256 * GIB) == MAX_WORKERS

    def test_returns_one_when_memory_covers_no_worker(self):
        assert suggest_concurrency(cpu_count=4, memory_bytes=SIBLING_MEMORY_BYTES) == 1

    def test_returns_one_when_memory_is_below_the_reserve(self):
        assert suggest_concurrency(cpu_count=4, memory_bytes=64 * 1024 * 1024) == 1

    def test_ignores_memory_when_undetectable(self, monkeypatch):
        monkeypatch.setattr(hardware, "detect_memory_bytes", lambda: None)

        assert suggest_concurrency(cpu_count=1) == 3

    @pytest.mark.parametrize("cpus", [1, 2, 4, 8, 16, 64])
    def test_stays_within_bounds_for_any_cpu_count(self, cpus):
        workers = suggest_concurrency(cpu_count=cpus, memory_bytes=8 * GIB)

        assert 1 <= workers <= MAX_WORKERS
