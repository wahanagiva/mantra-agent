"""
Mantra Creative Agent — /api/health handler.

Mirrors VPS gpu_api_server.py:326-364 with LOCAL improvements:
  * Real GPU detection (no Blackwell hardcode — user PCs typically have RTX 30/40
    series which DO work with PyTorch CUDA).
  * `mode` reflects actual runtime: "GPU (CUDA)" / "CPU" / "CPU (forced)".
  * Phase 2 introspection: reports gfpgan/ytdlp/ffmpeg/node availability.
  * Adds `agent_version` and `is_local_agent: True` for PHP smart-detect.

Response is BACKWARDS COMPATIBLE with VPS shape — only adds new fields.

Performance (H1 fix, P0):
  The "expensive" checks below (yt-dlp/ffmpeg subprocesses, rembg/gfpgan/torch
  imports) are probed ONCE at module import and cached in module-level globals
  with a 5-minute TTL. Subsequent /api/health calls just read the cache (a few
  microseconds). If the cache goes stale, the next request re-probes off-loop
  via asyncio.to_thread under an asyncio.Lock so a stampede of concurrent calls
  triggers exactly one probe.

  The web app polls /api/health every 3-10s; previously that meant 1.1-1.2s of
  blocking subprocess work per call which starved the event loop. Now those
  polls are sub-millisecond reads.

  Live (uncached) fields: timestamp, uptime_seconds, memory_rss_mb — all cheap.
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter

import config

router = APIRouter()
logger = logging.getLogger("mantra.agent.health")


# ── Probe-result cache ────────────────────────────────────────────────────────
# TTL chosen at 5 minutes: long enough that polling at 3-10s intervals is
# essentially free, short enough that a tool install/upgrade surfaces within a
# few minutes without restarting the agent.
_PROBE_TTL_SECONDS = 300.0

# Snapshot of all the slow-to-compute fields. Populated by `_probe_all()` —
# the first call is synchronous (eager at module import), subsequent stale
# refreshes happen off the event loop.
_probe_cache: dict = {}
_probe_cache_at: float = 0.0  # time.monotonic() of last successful probe
_probe_lock = asyncio.Lock()  # serialises off-loop re-probes


def _probe_all() -> dict:
    """
    Run every expensive check once and return a snapshot dict.

    Safe to call from any thread — uses only stdlib + thread-safe imports.
    Total wall time on a healthy machine: ~1-1.5s (dominated by yt-dlp +
    ffmpeg subprocess startup + first torch import).
    """
    snap: dict = {}

    # ── GPU detection (torch.cuda is a fast C call — no subprocess) ───────────
    snap["gpu_detected"] = False
    snap["gpu_name"] = None
    try:
        import torch
        snap["gpu_detected"] = bool(torch.cuda.is_available())
        if snap["gpu_detected"]:
            try:
                snap["gpu_name"] = torch.cuda.get_device_name(0)
            except Exception:
                snap["gpu_name"] = None
    except ImportError:
        pass
    except Exception:
        # Any other torch failure (broken install, etc.) — degrade gracefully
        pass

    # ── rembg availability ────────────────────────────────────────────────────
    try:
        import rembg  # noqa: F401
        snap["rembg_available"] = True
    except Exception as _rembg_e:
        # Log full exception so we can diagnose frozen-runtime import failures
        # (silent False here was hiding bugs — see 2026-04-26 debug session).
        import logging, traceback
        logging.getLogger("mantra.agent.health").warning(
            "rembg import failed: %s\n%s", _rembg_e, traceback.format_exc()
        )
        snap["rembg_available"] = False

    # ── GFPGAN availability ───────────────────────────────────────────────────
    try:
        import gfpgan  # noqa: F401
        snap["gfpgan_available"] = True
    except Exception:
        snap["gfpgan_available"] = False

    # ── yt-dlp availability (subprocess: yt-dlp --version) ────────────────────
    snap["ytdlp_available"] = False
    snap["ytdlp_version"] = None
    try:
        from handlers.youtube import is_ytdlp_available
        snap["ytdlp_available"], snap["ytdlp_version"] = is_ytdlp_available()
    except Exception:
        pass

    # ── ffmpeg availability (subprocess: ffmpeg -version) ─────────────────────
    snap["ffmpeg_available"] = False
    snap["ffmpeg_version"] = None
    try:
        from utils.ffmpeg_check import check_ffmpeg
        snap["ffmpeg_available"], snap["ffmpeg_version"] = check_ffmpeg()
    except Exception:
        pass

    # ── Node.js (already probed once at utils.node_check import — cheap) ──────
    snap["node_available"] = False
    snap["node_version"] = None
    try:
        from utils.node_check import NODE_AVAILABLE, NODE_VERSION
        snap["node_available"] = NODE_AVAILABLE
        snap["node_version"] = NODE_VERSION
    except Exception:
        pass

    return snap


def _seed_cache() -> None:
    """Populate the cache eagerly at module import so the FIRST request is fast too."""
    global _probe_cache, _probe_cache_at
    try:
        t0 = time.monotonic()
        _probe_cache = _probe_all()
        _probe_cache_at = time.monotonic()
        logger.info(
            "health probe seeded in %.2fs (ytdlp=%s, ffmpeg=%s, node=%s, gpu=%s, rembg=%s, gfpgan=%s)",
            _probe_cache_at - t0,
            _probe_cache.get("ytdlp_available"),
            _probe_cache.get("ffmpeg_available"),
            _probe_cache.get("node_available"),
            _probe_cache.get("gpu_detected"),
            _probe_cache.get("rembg_available"),
            _probe_cache.get("gfpgan_available"),
        )
    except Exception as e:
        # Never block module import on probe failure — leave cache empty,
        # the request handler will trigger a refresh.
        logger.warning("initial health probe failed: %s", e)
        _probe_cache = {}
        _probe_cache_at = 0.0


# Seed at import — runs once when FastAPI loads this router (before serving).
_seed_cache()


async def _get_probe_snapshot() -> dict:
    """
    Return a probe snapshot, refreshing off-loop if older than TTL.

    Concurrency model: holding `_probe_lock` while re-probing means a stampede
    of N concurrent stale-cache requests triggers EXACTLY ONE probe; the rest
    block on the lock for a few ms then read the freshly-seeded cache.
    """
    global _probe_cache, _probe_cache_at

    age = time.monotonic() - _probe_cache_at
    if _probe_cache and age < _PROBE_TTL_SECONDS:
        return _probe_cache

    async with _probe_lock:
        # Re-check under lock — another coroutine may have just refreshed.
        age = time.monotonic() - _probe_cache_at
        if _probe_cache and age < _PROBE_TTL_SECONDS:
            return _probe_cache
        # Off-load to thread pool so the event loop stays responsive.
        snap = await asyncio.to_thread(_probe_all)
        _probe_cache = snap
        _probe_cache_at = time.monotonic()
        return _probe_cache


@router.get("/api/health")
async def health_check():
    snap = await _get_probe_snapshot()

    # Derived fields (cheap, always live)
    gpu_detected = bool(snap.get("gpu_detected"))
    gpu_supported = gpu_detected and not config.FORCE_CPU
    if config.FORCE_CPU:
        mode = "CPU (forced)"
    elif gpu_supported:
        mode = "GPU (CUDA)"
    else:
        mode = "CPU"

    # ── Light system stats for tray badge — cheap, kept LIVE not cached ───────
    # Uses get_lite_stats (NOT get_system_stats) — the latter calls
    # psutil.Process.open_files() which on Windows scans every open handle
    # and burns ~300ms. The lite path is sub-millisecond.
    uptime_seconds: Optional[float] = None
    memory_rss_mb: Optional[float] = None
    try:
        from utils.system_stats import get_lite_stats
        s = get_lite_stats()
        uptime_seconds = s.get("uptime_seconds")
        memory_rss_mb = s.get("memory_rss_mb")
    except Exception:
        pass

    return {
        "status": "healthy",
        "rembg_available": snap.get("rembg_available", False),
        "gfpgan_available": snap.get("gfpgan_available", False),
        "ytdlp_available": snap.get("ytdlp_available", False),
        "ytdlp_version": snap.get("ytdlp_version"),
        "gpu_detected": gpu_detected,
        "gpu_supported": gpu_supported,
        "gpu_name": snap.get("gpu_name"),
        "mode": mode,
        "batch_max_urls": config.MAX_BATCH_URLS,
        "batch_max_concurrent": config.MAX_CONCURRENT_DOWNLOADS,
        "timestamp": datetime.now().isoformat(),
        # ── Local-agent extensions (backwards compatible) ─────────────────────
        "agent_version": config.APP_VERSION,
        "is_local_agent": True,
        # Phase 2 system-tool introspection
        "ffmpeg_available": snap.get("ffmpeg_available", False),
        "ffmpeg_version": snap.get("ffmpeg_version"),
        "node_available": snap.get("node_available", False),
        "node_version": snap.get("node_version"),
        # Phase 3 light runtime stats (admin/system has full breakdown)
        "uptime_seconds": uptime_seconds,
        "memory_rss_mb": memory_rss_mb,
    }
