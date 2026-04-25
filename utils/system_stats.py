"""
System stats helper for /api/health and admin endpoints.

Exposes:
  * get_system_stats() -> dict
      uptime_seconds, memory_rss_mb, memory_percent, cpu_percent,
      disk_free_gb (of APP_DATA_DIR), disk_used_gb, disk_total_gb,
      thread_count, open_files_count

  * get_queue_stats() -> dict
      merge_jobs_total, merge_jobs_processing, merge_jobs_done,
      merge_jobs_error, queue_size_bytes (MERGE_SERVE_DIR contents)

Both are SAFE to call from the /api/health handler (no blocking I/O beyond
a millisecond stat syscall).
"""
import logging
import os
import time
from pathlib import Path

import config

logger = logging.getLogger("mantra.agent.sysstats")

# Process start time (captured at module import)
_PROCESS_STARTED_AT = time.time()


def _try_import_psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes under path. Returns 0 if path missing or on error."""
    try:
        total = 0
        for p in path.iterdir():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except Exception:
                pass
        return total
    except Exception:
        return 0


def get_lite_stats() -> dict:
    """
    Cheap subset of get_system_stats() — uptime + memory_rss_mb only.

    Built for /api/health where the endpoint is polled every 3-10s and any
    blocking call adds up. Avoids `proc.open_files()` (which on Windows scans
    every open handle and takes ~300ms) and skips disk_usage. Total cost on
    a healthy machine: <1ms.
    """
    out: dict = {"uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1)}
    ps = _try_import_psutil()
    if ps is None:
        return out
    try:
        proc = ps.Process(os.getpid())
        out["memory_rss_mb"] = round(proc.memory_info().rss / 1024**2, 1)
    except Exception as e:
        logger.debug("get_lite_stats memory read failed: %s", e)
    return out


def get_system_stats() -> dict:
    """Process + disk stats. Safe if psutil missing."""
    ps = _try_import_psutil()

    stats: dict = {
        "uptime_seconds": round(time.time() - _PROCESS_STARTED_AT, 1),
        "psutil_available": ps is not None,
    }

    # Disk - use shutil.disk_usage (stdlib) so this always works
    try:
        import shutil
        du = shutil.disk_usage(str(config.APP_DATA_DIR))
        stats["disk_total_gb"] = round(du.total / 1024**3, 2)
        stats["disk_used_gb"] = round(du.used / 1024**3, 2)
        stats["disk_free_gb"] = round(du.free / 1024**3, 2)
    except Exception as e:
        logger.debug("disk_usage failed: %s", e)

    if ps is None:
        return stats

    # psutil-backed fields
    try:
        proc = ps.Process(os.getpid())
        mem = proc.memory_info()
        stats.update({
            "memory_rss_mb": round(mem.rss / 1024**2, 1),
            "memory_vms_mb": round(mem.vms / 1024**2, 1),
            "memory_percent": round(proc.memory_percent(), 2),
            "cpu_percent": round(proc.cpu_percent(interval=None), 1),  # non-blocking
            "thread_count": proc.num_threads(),
        })
        try:
            stats["open_files_count"] = len(proc.open_files())
        except Exception:
            stats["open_files_count"] = None
    except Exception as e:
        logger.debug("psutil process stats failed: %s", e)

    return stats


def get_queue_stats() -> dict:
    """Merge-job queue stats. Reads `handlers.merge.merge_jobs` if importable."""
    queue: dict = {
        "merge_jobs_total": 0,
        "merge_jobs_processing": 0,
        "merge_jobs_done": 0,
        "merge_jobs_error": 0,
        "queue_size_bytes": 0,
    }

    # Try to read merge state - deferred import to avoid circularity
    try:
        from handlers import merge as merge_mod
        with merge_mod.merge_jobs_lock:
            jobs = list(merge_mod.merge_jobs.values())
        queue["merge_jobs_total"] = len(jobs)
        for j in jobs:
            st = j.get("status", "unknown")
            if st == "processing":
                queue["merge_jobs_processing"] += 1
            elif st == "done":
                queue["merge_jobs_done"] += 1
            elif st == "error":
                queue["merge_jobs_error"] += 1
    except Exception as e:
        logger.debug("queue_stats read failed: %s", e)

    # Disk size of MERGE_SERVE_DIR
    try:
        queue["queue_size_bytes"] = _dir_size_bytes(Path(str(config.MERGE_SERVE_DIR)))
        queue["queue_size_mb"] = round(queue["queue_size_bytes"] / 1024**2, 2)
    except Exception as e:
        logger.debug("queue disk-size failed: %s", e)

    return queue


def get_all_stats() -> dict:
    """One-shot convenience - returns `{system: ..., queue: ...}`."""
    return {
        "system": get_system_stats(),
        "queue": get_queue_stats(),
    }
