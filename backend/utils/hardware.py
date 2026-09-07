"""Size the web server to the hardware the container is actually allowed to use.

Run as ``python3 -m utils.hardware`` to print the worker count that
``WEB_SERVER_CONCURRENCY=auto`` resolves to.
"""

import math
import os
from pathlib import Path
from typing import Final

# Gunicorn's stock guidance for a mostly I/O-bound app, and what RomM has long
# recommended setting by hand.
WORKERS_PER_CPU: Final = 2

# Conservative estimate of a warmed-up worker: gunicorn runs without --preload,
# so every worker imports the app independently and shares little of it.
WORKER_MEMORY_BYTES: Final = 300 * 1024 * 1024

# valkey, nginx, both RQ workers (each carrying its own copy of the app) and the
# watchers share the container, so their memory is not the web server's to spend.
SIBLING_MEMORY_BYTES: Final = 1024 * 1024 * 1024

# Every worker's pool holds up to pool_size + max_overflow connections, 15 by
# SQLAlchemy default, against a stock MariaDB max_connections of 151.
MAX_WORKERS: Final = 8

# cgroup v1 reports a sentinel near 2^63 rather than "max" when memory is
# unlimited.
_UNLIMITED_THRESHOLD: Final = 1 << 62


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _quota_cores(quota: int | None, period: int | None) -> float | None:
    """Cores a cfs quota/period pair allows, or None when it sets no limit."""
    if not quota or not period or quota <= 0 or period <= 0:
        return None
    return quota / period


def _cgroup_cpu_quota() -> float | None:
    """Cores the cgroup permits, or None when unlimited or unreadable."""
    if (v2 := _read_text("/sys/fs/cgroup/cpu.max")) is not None:
        # "<quota> <period>", where an unlimited quota reads "max".
        quota, _, period = v2.partition(" ")
        return _quota_cores(_to_int(quota), _to_int(period))

    # cgroup v1 spreads the pair across two files and writes -1 for unlimited.
    return _quota_cores(
        _to_int(_read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")),
        _to_int(_read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")),
    )


def _cgroup_memory_limit() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        limit = _to_int(_read_text(path))
        if limit is not None and 0 < limit < _UNLIMITED_THRESHOLD:
            return limit
    return None


def _physical_memory() -> int | None:
    for line in (_read_text("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            kilobytes = _to_int(fields[1]) if len(fields) > 1 else None
            return kilobytes * 1024 if kilobytes else None
    return None


def detect_cpu_count() -> int:
    """Cores this process may use, honoring CPU affinity and any cgroup quota."""
    # process_cpu_count honors --cpuset-cpus, which cpu_count ignores.
    count = os.process_cpu_count() or os.cpu_count() or 1
    if (quota := _cgroup_cpu_quota()) is not None:
        count = min(count, math.ceil(quota))
    return max(1, count)


def detect_memory_bytes() -> int | None:
    """Memory this container may use, or None when nothing reports a figure."""
    # A cgroup limit can exceed physical RAM, so neither alone is the budget.
    limits = [limit for limit in (_cgroup_memory_limit(), _physical_memory()) if limit]
    return min(limits) if limits else None


def suggest_concurrency(
    cpu_count: int | None = None, memory_bytes: int | None = None
) -> int:
    """Worker count that fits the detected CPU allowance and memory budget."""
    cpus = detect_cpu_count() if cpu_count is None else cpu_count
    memory = detect_memory_bytes() if memory_bytes is None else memory_bytes

    workers = WORKERS_PER_CPU * cpus + 1
    if memory is not None:
        affordable = (memory - SIBLING_MEMORY_BYTES) // WORKER_MEMORY_BYTES
        workers = min(workers, affordable)

    return max(1, min(workers, MAX_WORKERS))


if __name__ == "__main__":
    print(suggest_concurrency())
