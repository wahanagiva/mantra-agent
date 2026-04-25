"""
Mantra Creative Agent — YouTube album-merge handlers.

Mirrors VPS gpu_api_server.py:
  * Lines 803-826  — YoutubeMergeRequest model + module-level state
  * Lines 828-921  — _run_merge_job background coroutine
  * Lines 924-942  — POST /api/youtube-mp3-merge
  * Lines 945-956  — GET  /api/merge-status/{job_id}
  * Lines 960-983  — GET  /api/serve-merge/{serve_id}
  * Lines 987-1021 — _cleanup_loop (hourly sweep, 2h TTL)
  * Lines 1024-1028 — startup hook wiring the cleanup loop

LOCAL improvements over VPS:
  * Cross-platform paths: tempfile uses config.TEMP_DIR (not hardcoded /tmp/),
    serve dir comes from config.MERGE_SERVE_DIR (pathlib.Path on user's OS).
  * Windows-aware ffmpeg discovery: env override → imageio_ffmpeg → PATH fallback,
    so users without ffmpeg on PATH still work if imageio-ffmpeg is installed.
  * Deferred import of handlers.youtube.download_single_youtube_mp3 inside
    _run_merge_job so this module imports even if youtube.py isn't ready
    (avoids circular-import / build-order issues).

Behavior preserved verbatim from VPS:
  * `chr(39)` and `chr(10)` in the ffmpeg concat file (avoids quote-escape bugs).
  * Module-level ThreadPoolExecutor shared across requests (vs per-call).
  * Status dict returned RAW (no envelope) from /api/merge-status — matches VPS.
  * Background cleanup-on-download for served MP3 + meta sidecar.
  * Failed downloads are tracked but don't fail the whole job (best-effort).
"""
import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse

import config
from models.schemas import YoutubeMergeRequest
from utils.errors import format_for_response

router = APIRouter()
logger = logging.getLogger("mantra.agent.merge")

# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress flashing CMD
# window when frozen GUI app (no console) spawns ffmpeg.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ── ffmpeg path resolution (Windows-aware) ────────────────────────────────────
def _resolve_ffmpeg_path() -> str:
    """
    Resolve ffmpeg binary path with three-tier fallback.

    Priority:
      1. config.FFMPEG_PATH env override (MANTRA_AGENT_FFMPEG)
      2. imageio_ffmpeg.get_ffmpeg_exe() — bundled binary if package installed
      3. "ffmpeg" — assume on PATH (matches VPS behavior)
    """
    if config.FFMPEG_PATH:
        return config.FFMPEG_PATH
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    except Exception:
        pass
    return "ffmpeg"


FFMPEG_PATH = _resolve_ffmpeg_path()
logger.info("ffmpeg path: %s", FFMPEG_PATH)


# ── Module-level state ────────────────────────────────────────────────────────
merge_jobs: dict = {}  # job_id -> status dict
merge_jobs_lock = threading.Lock()
# Global pool — shared across all merge requests.
# Was 4 (hardcoded, ignored config); dropped to match config.MAX_CONCURRENT_DOWNLOADS=3.
# Each worker fires 2 subprocess rounds (info + download), so effective
# concurrent yt-dlp handshakes against YouTube = 2 × max_workers. At 4 that
# was ~8 — enough to trip YouTube's anti-abuse on ~1-in-15 tracks. Dropping
# to 3 brings it to ~6 which is well within safe budget.
merge_executor = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_DOWNLOADS)

# Seconds to wait between serial retry attempts (per track) for tracks that
# failed in the initial parallel pass. Gives YouTube's rate-limiter time to
# forget about us.
RETRY_INITIAL_DELAY_S = 3.0
RETRY_MAX_ATTEMPTS = 2  # one parallel attempt + two serial retries = 3 total chances


# ── Background job ────────────────────────────────────────────────────────────
async def _run_merge_job(job_id: str, urls: list, titles: list, album_name: str):
    """Background coroutine: download + merge + save. Updates merge_jobs[job_id]."""
    # Deferred import to avoid hard dependency at module load time
    # (handlers.youtube may be built in parallel).
    from handlers.youtube import download_single_youtube_mp3

    temp_dir = tempfile.mkdtemp(prefix="yt_merge_", dir=str(config.TEMP_DIR))
    try:
        logger.info("[Merge] Job %s: downloading %d tracks...", job_id[:8], len(urls))
        loop = asyncio.get_event_loop()
        futures = [
            loop.run_in_executor(merge_executor, download_single_youtube_mp3, u)
            for u in urls
        ]
        results = await asyncio.gather(*futures, return_exceptions=True)

        # Track results keyed by ORIGINAL list index so we preserve track order
        # (`track_%03d.mp3`) across the retry pass below.
        track_results: dict[int, object] = {}
        mp3_files: list[str] = []
        failed_urls: list[str] = []
        failed_indices: list[int] = []
        total_secs = 0

        for i, result in enumerate(results):
            if isinstance(result, Exception) or not result.success:
                err = str(result) if isinstance(result, Exception) else (result.error or "unknown")
                logger.warning(
                    "[Merge] Job %s: track %d FAILED on pass 1 — %s (will retry): %s",
                    job_id[:8], i, urls[i], err[:200],
                )
                failed_indices.append(i)
                continue
            track_results[i] = result
            total_secs += result.duration or 0

        # ── Serial retry pass for failed tracks ──────────────────────────────
        # Each attempt is SEQUENTIAL (1 at a time) with a growing backoff so we
        # give YouTube's rate-limiter time to cool off. Effective per-track
        # attempts: 1 (parallel pass) + RETRY_MAX_ATTEMPTS serial.
        still_failed: list[int] = list(failed_indices)
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            if not still_failed:
                break
            delay = RETRY_INITIAL_DELAY_S * attempt  # 3s, 6s
            logger.info(
                "[Merge] Job %s: retry pass %d — %d track(s) to recover (delay=%.1fs, serial)",
                job_id[:8], attempt, len(still_failed), delay,
            )
            await asyncio.sleep(delay)

            next_round: list[int] = []
            for i in still_failed:
                url = urls[i]
                logger.info("[Merge] Job %s: retry track %d: %s", job_id[:8], i, url)
                # Run on the default executor — SERIAL (one at a time) to avoid
                # re-triggering rate-limit.
                r = await loop.run_in_executor(None, download_single_youtube_mp3, url)
                if isinstance(r, Exception) or not r.success:
                    err = str(r) if isinstance(r, Exception) else (r.error or "unknown")
                    logger.warning(
                        "[Merge] Job %s: retry %d track %d still FAIL — %s",
                        job_id[:8], attempt, i, err[:200],
                    )
                    next_round.append(i)
                else:
                    logger.info("[Merge] Job %s: retry %d track %d RECOVERED", job_id[:8], attempt, i)
                    track_results[i] = r
                    total_secs += r.duration or 0
            still_failed = next_round

        # Tracks that exhausted all retries
        for i in still_failed:
            failed_urls.append(urls[i])

        # ── Materialize successful tracks to disk in ORIGINAL ORDER ──────────
        for i in sorted(track_results.keys()):
            result = track_results[i]
            mp3_path = os.path.join(temp_dir, "track_%03d.mp3" % i)
            with open(mp3_path, "wb") as fh:
                fh.write(base64.b64decode(result.audio_base64))
            mp3_files.append(mp3_path)
            logger.info("[Merge] Job %s: track %02d ready (idx=%d)", job_id[:8], len(mp3_files), i)

        if not mp3_files:
            with merge_jobs_lock:
                merge_jobs[job_id] = {
                    "status": "error",
                    "success": False,
                    "error": "Semua download gagal",
                    "failed_urls": failed_urls,
                }
            return

        concat_file = os.path.join(temp_dir, "concat.txt")
        output_path = os.path.join(temp_dir, "full_album.mp3")
        # NOTE: chr(39) = single quote, chr(10) = newline. Done verbatim from VPS
        # to avoid quote-escape bugs on paths containing apostrophes.
        with open(concat_file, "w", encoding="utf-8") as fh:
            for p in mp3_files:
                fh.write("file " + chr(39) + p + chr(39) + chr(10))

        logger.info("[Merge] Job %s: merging %d tracks...", job_id[:8], len(mp3_files))
        # Run ffmpeg in executor so we don't block the asyncio event loop
        merge_result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_file, "-ar", "44100", "-ab", "192k", output_path],
                capture_output=True, text=True, timeout=600,
                creationflags=_NO_WINDOW,
            )
        )

        if merge_result.returncode != 0 or not os.path.exists(output_path):
            err_tail = merge_result.stderr[-300:] if merge_result.stderr else "unknown"
            with merge_jobs_lock:
                merge_jobs[job_id] = {
                    "status": "error",
                    "success": False,
                    "error": "ffmpeg gagal: " + err_tail,
                    "failed_urls": failed_urls or [],
                }
            return

        filesize = os.path.getsize(output_path)
        hrs = total_secs // 3600
        mins = (total_secs % 3600) // 60
        secs = total_secs % 60
        dur_str = ("%02d:%02d:%02d" % (hrs, mins, secs)) if hrs else ("%02d:%02d" % (mins, secs))

        safe = "".join(c for c in album_name if c.isalnum() or c in " -()")
        filename = (safe.strip() or "Full Album") + ".mp3"

        serve_id = str(uuid.uuid4())
        serve_path = str(config.MERGE_SERVE_DIR / (serve_id + ".mp3"))
        meta_path = str(config.MERGE_SERVE_DIR / (serve_id + ".meta"))
        shutil.copy2(output_path, serve_path)
        with open(meta_path, "w", encoding="utf-8") as mf:
            mf.write(filename)

        logger.info(
            "[Merge] Job %s: done %d tracks, %s, %.1fMB -> %s",
            job_id[:8], len(mp3_files), dur_str, filesize / 1024 / 1024, serve_id,
        )
        with merge_jobs_lock:
            merge_jobs[job_id] = {
                "status":      "done",
                "success":     True,
                "download_id": serve_id,
                "filename":    filename,
                "duration":    dur_str,
                "track_count": len(mp3_files),
                "filesize":    filesize,
                "failed_urls": failed_urls or [],
            }

    except subprocess.TimeoutExpired:
        with merge_jobs_lock:
            merge_jobs[job_id] = {
                "status": "error",
                "success": False,
                "error": "Timeout (maks 10 menit)",
                "failed_urls": [],
            }
    except Exception as e:
        logger.exception("[Merge] Job %s: error", job_id[:8])
        with merge_jobs_lock:
            merge_jobs[job_id] = {
                "status": "error",
                "success": False,
                "error": format_for_response(e),
                "failed_urls": [],
            }
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/api/youtube-mp3-merge")
async def merge_youtube_mp3(request: YoutubeMergeRequest, background_tasks: BackgroundTasks):
    """Start async merge job — returns job_id immediately, no timeout."""
    if not request.urls:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "URL kosong"},
        )
    if len(request.urls) > config.MAX_MERGE_URLS:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"Maks {config.MAX_MERGE_URLS} URL"},
        )

    job_id = str(uuid.uuid4())
    with merge_jobs_lock:
        merge_jobs[job_id] = {"status": "processing", "started_at": time.time()}
    background_tasks.add_task(
        _run_merge_job, job_id,
        list(request.urls),
        list(request.titles or []),
        request.album_name or "Full Album",
    )
    logger.info("[Merge] Job %s: queued %d tracks", job_id[:8], len(request.urls))
    return {"success": True, "job_id": job_id}


@router.get("/api/merge-status/{job_id}")
async def get_merge_status(job_id: str):
    """Poll merge job status. Returns the status dict raw (no envelope)."""
    if not re.match(r"^[a-f0-9-]{36}$", job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    with merge_jobs_lock:
        job = merge_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/api/serve-merge/{serve_id}")
async def serve_merged_file(serve_id: str, background_tasks: BackgroundTasks):
    """Serve a previously merged MP3 file and delete it after download."""
    if not re.match(r"^[a-f0-9-]{36}$", serve_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    mp3_path = str(config.MERGE_SERVE_DIR / (serve_id + ".mp3"))
    meta_path = str(config.MERGE_SERVE_DIR / (serve_id + ".meta"))
    if not os.path.exists(mp3_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    filename = serve_id + ".mp3"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as mf:
                filename = mf.read().strip() or filename
        except Exception:
            pass

    def cleanup():
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        try:
            os.remove(meta_path)
        except Exception:
            pass

    background_tasks.add_task(cleanup)
    return FileResponse(mp3_path, media_type="audio/mpeg", filename=filename)


# ── Periodic cleanup ──────────────────────────────────────────────────────────
def _sweep_orphan_temp_dirs(max_age_seconds: int = 3600) -> int:
    """
    Delete orphan temp dirs left behind when the agent was killed mid-job.

    Targets two prefixes in config.TEMP_DIR:
      * yt_mp3_* / yt_mp3_batch_* — single-track downloads (youtube.py)
      * yt_merge_*                — album merge workspaces (this file)

    Only wipes dirs whose mtime is older than `max_age_seconds` (default 1h)
    so we never clobber an in-flight job from another agent instance.

    Returns the number of dirs removed.
    """
    now = time.time()
    temp_root = str(config.TEMP_DIR)
    removed = 0
    try:
        for name in os.listdir(temp_root):
            if not (name.startswith("yt_mp3_") or name.startswith("yt_merge_")):
                continue
            path = os.path.join(temp_root, name)
            try:
                if not os.path.isdir(path):
                    continue
                age = now - os.path.getmtime(path)
                if age < max_age_seconds:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.exists(path):
                    removed += 1
                    logger.info("[Cleanup] Removed orphan temp dir: %s (age=%ds)", name, int(age))
            except Exception as e:
                logger.debug("[Cleanup] skip %s: %s", name, e)
    except FileNotFoundError:
        # TEMP_DIR somehow missing — nothing to sweep
        pass
    except Exception:
        logger.exception("[Cleanup] orphan sweep failed")
    return removed


def _sweep_update_backup_dirs(max_age_seconds: int = 3600) -> int:
    """
    Delete leftover install-dir backups created by the auto-updater.

    `utils.updater.apply_patch` creates a `<install_dir>_backup_<unix_ts>`
    sibling before extracting a patch, then leaves it for ~1 hour as a manual
    rollback safety net. This sweep removes them after `max_age_seconds`.

    Looks in INSTALL_DIR's parent for siblings matching `<install_name>_backup_*`.
    Only fires when running as a frozen bundle (skip in dev mode).

    Returns the number of backup dirs removed.
    """
    if not getattr(sys, "frozen", False):
        return 0  # dev mode — no backups to sweep
    try:
        install_dir = Path(sys.executable).resolve().parent
    except Exception:
        return 0
    parent = install_dir.parent
    install_name = install_dir.name
    prefix = f"{install_name}_backup_"
    now = time.time()
    removed = 0
    try:
        for entry in parent.iterdir():
            if not entry.is_dir() or not entry.name.startswith(prefix):
                continue
            try:
                age = now - entry.stat().st_mtime
                if age < max_age_seconds:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                if not entry.exists():
                    removed += 1
                    logger.info("[Cleanup] Removed update backup: %s (age=%ds)", entry.name, int(age))
            except Exception as e:
                logger.debug("[Cleanup] skip backup %s: %s", entry.name, e)
    except (FileNotFoundError, PermissionError):
        pass
    except Exception:
        logger.exception("[Cleanup] update-backup sweep failed")
    return removed


async def _cleanup_loop():
    """
    Runs every hour:
      1. Delete old MERGE_SERVE_DIR files (>2h)
      2. Prune merge_jobs dict entries (>2h)
      3. Delete orphan temp dirs (yt_mp3_*, yt_merge_*, >1h)

    Best-effort — any exception is swallowed so the loop never dies.
    """
    while True:
        await asyncio.sleep(3600)  # jalankan setiap 1 jam
        try:
            now = time.time()
            ttl = 7200  # 2 jam

            # 1. Hapus file lama di MERGE_SERVE_DIR
            try:
                serve_dir = str(config.MERGE_SERVE_DIR)
                for fname in os.listdir(serve_dir):
                    fpath_c = os.path.join(serve_dir, fname)
                    try:
                        if os.path.getmtime(fpath_c) < now - ttl:
                            os.remove(fpath_c)
                            logger.info("[Cleanup] Hapus orphan serve: %s", fname)
                    except Exception:
                        pass
            except Exception:
                pass

            # 2. Hapus entry lama dari merge_jobs dict
            with merge_jobs_lock:
                expired = [
                    jid for jid, job in list(merge_jobs.items())
                    if now - job.get("started_at", now) > ttl
                ]
                for jid in expired:
                    merge_jobs.pop(jid, None)
            if expired:
                logger.info("[Cleanup] Hapus %d job entry lama dari memory", len(expired))

            # 3. Hapus orphan temp dirs (yt_mp3_*, yt_merge_*, umur >1 jam)
            _sweep_orphan_temp_dirs(max_age_seconds=3600)

            # 4. Hapus auto-updater backup dirs (umur >1 jam, frozen mode only)
            _sweep_update_backup_dirs(max_age_seconds=3600)

        except Exception:
            pass


# ── Startup wiring ────────────────────────────────────────────────────────────
def register_startup(app):
    """
    Wire the periodic cleanup loop onto the FastAPI app's startup event.

    Called by main.py after include_router() so we can register cleanly without
    requiring this module to know about the app at import time.

    Also runs a one-shot orphan sweep IMMEDIATELY at startup so temp dirs
    left behind by a previous agent crash/kill get cleaned within seconds of
    restart — not stuck until the first hourly loop fires.
    """
    @app.on_event("startup")
    async def _startup():
        # One-shot sweep at boot — catches orphans from previous run
        removed = _sweep_orphan_temp_dirs(max_age_seconds=0)
        if removed:
            logger.info("[Startup] Swept %d orphan temp dir(s) from previous run", removed)
        asyncio.create_task(_cleanup_loop())
        logger.info("Periodic cleanup loop started (interval: 1h, TTL: 2h)")
