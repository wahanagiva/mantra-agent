"""
Mantra Creative Agent — YouTube endpoints.

Mirrors VPS gpu_api_server.py:
  * Lines 192-323 — download_single_youtube_mp3() worker
  * Lines 366-419 — GET  /api/youtube-info
  * Lines 421-549 — POST /api/youtube-mp3
  * Lines 552-633 — POST /api/youtube-mp3-batch

Implementation notes:
  * Uses yt-dlp via SUBPROCESS (NOT the Python API) to stay close to VPS.
  * yt-dlp path comes from config.YTDLP_PATH (defaults to "yt-dlp" — PATH-resolved).
  * Temp dirs use config.TEMP_DIR via tempfile.mkdtemp (cross-platform).
  * Subprocess calls pass encoding="utf-8", errors="replace" so non-UTF-8
    Windows locales don't blow up on decode.
  * yt-dlp --js-runtimes node REQUIRES Node.js on PATH; we log a warning at
    module import if missing but don't crash (user can install Node + restart).
  * download_single_youtube_mp3 is module-level so handlers/merge.py can import it.
  * Batch handler uses a LOCAL ThreadPoolExecutor (matches VPS pattern). The
    merge handler (separate file) owns its own pool — don't share.
"""
import asyncio
import base64
import json
import logging
import os
import shutil  # noqa: F401  (kept for future use; VPS uses manual cleanup)
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query

import config
from models.schemas import (
    YoutubeBatchItemResult,
    YoutubeBatchRequest,
    YoutubeBatchResponse,
    YoutubeInfoResponse,
    YoutubeMp3Request,
    YoutubeMp3Response,
)
from utils.errors import format_for_response

router = APIRouter()
logger = logging.getLogger("mantra.agent.youtube")

# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress flashing CMD
# window when frozen GUI app (no console) spawns yt-dlp / node.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ── Node.js availability check (yt-dlp uses --js-runtimes node) ───────────────
# Probe is DEFERRED to first call — see utils.node_check.get_node_status().
# Module-level NODE_AVAILABLE alias is kept lazy via PEP 562 __getattr__ so
# health.py's existing import keeps working.
def _check_node() -> bool:
    """Legacy alias; defers to utils.node_check.get_node_status()."""
    from utils.node_check import get_node_status
    return get_node_status()[0]


def __getattr__(name: str):
    if name in ("_NODE_AVAILABLE", "NODE_AVAILABLE"):
        from utils.node_check import get_node_status
        return get_node_status()[0]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── yt-dlp availability helper (used by /api/health) ──────────────────────────
def is_ytdlp_available() -> tuple[bool, str | None]:
    """
    Probe yt-dlp by running `<YTDLP_PATH> --version`.
    Returns (available, version_str_or_None). Used by health endpoint.
    """
    try:
        r = subprocess.run(
            [config.YTDLP_PATH, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, r.stdout.strip()
    except Exception:
        pass
    return False, None


# ── Worker: single-URL MP3 download (exported for merge.py) ───────────────────
def download_single_youtube_mp3(url: str) -> YoutubeBatchItemResult:
    """
    Download a single YouTube video as MP3.
    Designed to be run in a ThreadPoolExecutor.

    Mirrors VPS gpu_api_server.py:192-323 — same flags, same flow, same error
    envelope. Only deviation: temp dir created under config.TEMP_DIR instead
    of /tmp, and subprocess decoding is explicit UTF-8 with replacement.

    Returns YoutubeBatchItemResult with audio_base64 on success or error on
    failure. Never raises (caller uses asyncio.gather(return_exceptions=True)
    as a belt-and-braces backstop).
    """
    temp_dir = None

    try:
        # Step 1: fetch video info to validate duration before downloading
        info_cmd = [
            config.YTDLP_PATH,
            "--dump-json",
            "--no-download",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=tv,mweb",
            "--retries", "3",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--force-ipv4",
            url,
        ]

        info_result = subprocess.run(
            info_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=_NO_WINDOW,
        )

        if info_result.returncode != 0:
            error_msg = info_result.stderr.strip() if info_result.stderr else "Failed to fetch video info"
            # CRITICAL: log the stderr so we can diagnose why info fetch failed.
            # Previously this was silent — failures were invisible in agent.log.
            logger.warning(
                "[Batch] yt-dlp info FAIL (rc=%d) for %s — stderr: %s",
                info_result.returncode, url, error_msg[:500],
            )
            return YoutubeBatchItemResult(url=url, success=False, error=format_for_response(error_msg))

        info = json.loads(info_result.stdout)
        duration = info.get("duration", 0)
        title = info.get("title", "Unknown")

        # Enforce duration ceiling
        if duration > config.MAX_DURATION_SECONDS:
            return YoutubeBatchItemResult(
                url=url,
                success=False,
                error=f"Video too long: {duration}s (max {config.MAX_DURATION_SECONDS}s / 10 minutes)",
            )

        logger.info("[Batch] Downloading: %s (%ss)", title, duration)

        # Step 2: prepare temp workspace
        temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR), prefix="yt_mp3_batch_")
        output_template = os.path.join(temp_dir, "audio.%(ext)s")

        # Step 3: download + convert to MP3
        # --sleep-interval + --max-sleep-interval: yt-dlp sleeps 1-3s between
        # internal HTTP hits within a single download — smooths burst on the
        # fragment fetch loop so YouTube's anti-bot is less likely to trip.
        # --throttled-rate: if YouTube starts throttling us, abort early and
        # let the download retry logic handle it instead of hanging.
        download_cmd = [
            config.YTDLP_PATH,
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--output", output_template,
            "--no-keep-video",
            "--quiet",
            "--extractor-args", "youtube:player_client=tv,mweb",
            "--retries", "5",
            "--fragment-retries", "5",
            "--sleep-interval", "1",
            "--max-sleep-interval", "3",
            "--throttled-rate", "100K",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--force-ipv4",
            url,
        ]

        download_result = subprocess.run(
            download_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,  # 5 minute ceiling per download
            creationflags=_NO_WINDOW,
        )

        if download_result.returncode != 0:
            error_msg = download_result.stderr.strip() if download_result.stderr else "Failed to download audio"
            # CRITICAL: log the stderr so we know WHY the download failed
            # (403 / 429 / signature / etc.). Previously silent.
            logger.warning(
                "[Batch] yt-dlp download FAIL (rc=%d) for %s — stderr: %s",
                download_result.returncode, url, error_msg[:500],
            )
            return YoutubeBatchItemResult(url=url, success=False, error=format_for_response(error_msg))

        # Locate the produced MP3 file
        mp3_file = os.path.join(temp_dir, "audio.mp3")

        if not os.path.exists(mp3_file):
            # Fall back to scanning the dir for any .mp3
            for f in os.listdir(temp_dir):
                if f.endswith(".mp3"):
                    mp3_file = os.path.join(temp_dir, f)
                    break

        if not os.path.exists(mp3_file):
            return YoutubeBatchItemResult(url=url, success=False, error="MP3 file not found after conversion")

        # Read + encode payload
        filesize = os.path.getsize(mp3_file)

        with open(mp3_file, "rb") as f:
            audio_data = f.read()

        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

        # Free the file early; finally-block sweeps the rest
        os.remove(mp3_file)

        logger.info("[Batch] Completed: %s", title)

        return YoutubeBatchItemResult(
            url=url,
            success=True,
            audio_base64=audio_base64,
            title=title,
            duration=duration,
            filesize=filesize,
        )

    except subprocess.TimeoutExpired:
        return YoutubeBatchItemResult(url=url, success=False, error="Timeout downloading audio (max 5 minutes)")
    except json.JSONDecodeError:
        return YoutubeBatchItemResult(url=url, success=False, error="Failed to parse video info")
    except Exception as e:
        logger.exception("[Batch] Error for %s", url)
        return YoutubeBatchItemResult(url=url, success=False, error=format_for_response(e))
    finally:
        # File-by-file cleanup matches VPS exactly (don't refactor to rmtree)
        if temp_dir and os.path.exists(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    os.remove(os.path.join(temp_dir, f))
                os.rmdir(temp_dir)
            except Exception:
                pass


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("/api/youtube-info", response_model=YoutubeInfoResponse)
async def get_youtube_info(url: str = Query(..., description="YouTube video URL")):
    """
    Get YouTube video information without downloading.
    Mirrors VPS gpu_api_server.py:366-419.
    """
    try:
        cmd = [
            config.YTDLP_PATH,
            "--dump-json",
            "--no-download",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=tv,mweb",
            "--retries", "3",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--force-ipv4",
            url,
        ]

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                creationflags=_NO_WINDOW,
            ),
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Failed to fetch video info"
            return YoutubeInfoResponse(success=False, error=format_for_response(error_msg))

        info = json.loads(result.stdout)

        duration = info.get("duration", 0)
        if duration > config.MAX_DURATION_SECONDS:
            return YoutubeInfoResponse(
                success=False,
                error=f"Video too long: {duration}s (max {config.MAX_DURATION_SECONDS}s / 10 minutes)",
            )

        return YoutubeInfoResponse(
            success=True,
            title=info.get("title"),
            duration=duration,
            thumbnail=info.get("thumbnail"),
            uploader=info.get("uploader"),
            view_count=info.get("view_count"),
            description=info.get("description", "")[:500],  # match VPS truncation
        )

    except subprocess.TimeoutExpired:
        return YoutubeInfoResponse(success=False, error="Timeout fetching video info")
    except json.JSONDecodeError:
        return YoutubeInfoResponse(success=False, error="Failed to parse video info")
    except Exception as e:
        logger.exception("youtube-info error")
        return YoutubeInfoResponse(success=False, error=format_for_response(e))


@router.post("/api/youtube-mp3", response_model=YoutubeMp3Response)
async def download_youtube_mp3(request: YoutubeMp3Request):
    """
    Download YouTube video and convert to MP3.
    Mirrors VPS gpu_api_server.py:421-549.
    """
    start_time = time.time()
    temp_dir = None

    try:
        # Step 1: probe info for duration check
        info_cmd = [
            config.YTDLP_PATH,
            "--dump-json",
            "--no-download",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=tv,mweb",
            "--retries", "3",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--force-ipv4",
            request.url,
        ]

        loop = asyncio.get_event_loop()
        info_result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                info_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                creationflags=_NO_WINDOW,
            ),
        )

        if info_result.returncode != 0:
            error_msg = info_result.stderr.strip() if info_result.stderr else "Failed to fetch video info"
            return YoutubeMp3Response(success=False, error=format_for_response(error_msg))

        info = json.loads(info_result.stdout)
        duration = info.get("duration", 0)
        title = info.get("title", "Unknown")

        # Reject livestreams BEFORE duration check — yt-dlp reports duration=0
        # for live content, so the >MAX guard misses them and we'd hang on the
        # download subprocess until its 5-minute timeout. duration in (None, 0)
        # also catches indeterminate-length / unprobeable videos.
        if (
            info.get("is_live") is True
            or info.get("live_status") == "is_live"
            or duration in (None, 0)
        ):
            return YoutubeMp3Response(
                success=False,
                error="Live streams are not supported. Please provide a finished video URL.",
            )

        if duration > config.MAX_DURATION_SECONDS:
            return YoutubeMp3Response(
                success=False,
                error=f"Video too long: {duration}s (max {config.MAX_DURATION_SECONDS}s / 10 minutes)",
            )

        logger.info("Downloading YouTube audio: %s (%ss)", title, duration)

        # Step 2: workspace
        temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR), prefix="yt_mp3_")
        output_template = os.path.join(temp_dir, "audio.%(ext)s")

        # Step 3: download + convert
        download_cmd = [
            config.YTDLP_PATH,
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "192K",
            "--output", output_template,
            "--no-keep-video",
            "--quiet",
            "--extractor-args", "youtube:player_client=tv,mweb",
            "--retries", "3",
            "--fragment-retries", "3",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--force-ipv4",
            request.url,
        ]

        download_result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                download_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=_NO_WINDOW,
            ),
        )

        if download_result.returncode != 0:
            error_msg = download_result.stderr.strip() if download_result.stderr else "Failed to download audio"
            return YoutubeMp3Response(success=False, error=format_for_response(error_msg))

        # Locate output
        mp3_file = os.path.join(temp_dir, "audio.mp3")

        if not os.path.exists(mp3_file):
            for f in os.listdir(temp_dir):
                if f.endswith(".mp3"):
                    mp3_file = os.path.join(temp_dir, f)
                    break

        if not os.path.exists(mp3_file):
            return YoutubeMp3Response(success=False, error="MP3 file not found after conversion")

        filesize = os.path.getsize(mp3_file)

        with open(mp3_file, "rb") as f:
            audio_data = f.read()

        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

        os.remove(mp3_file)

        processing_time = time.time() - start_time
        logger.info("YouTube MP3 completed in %.2fs: %s", processing_time, title)

        return YoutubeMp3Response(
            success=True,
            audio_base64=audio_base64,
            title=title,
            duration=duration,
            filesize=filesize,
            processing_time=processing_time,
        )

    except subprocess.TimeoutExpired:
        return YoutubeMp3Response(success=False, error="Timeout downloading audio (max 5 minutes)")
    except json.JSONDecodeError:
        return YoutubeMp3Response(success=False, error="Failed to parse video info")
    except Exception as e:
        logger.exception("YouTube MP3 error")
        return YoutubeMp3Response(success=False, error=format_for_response(e))
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    os.remove(os.path.join(temp_dir, f))
                os.rmdir(temp_dir)
            except Exception:
                pass


@router.post("/api/youtube-mp3-batch", response_model=YoutubeBatchResponse)
async def download_youtube_mp3_batch(request: YoutubeBatchRequest):
    """
    Download multiple YouTube videos as MP3 in parallel.
    Mirrors VPS gpu_api_server.py:552-633.

    - Maximum MAX_BATCH_URLS (5) URLs per request
    - Maximum MAX_CONCURRENT_DOWNLOADS (3) concurrent workers
    - Each video must respect MAX_DURATION_SECONDS (15 min)
    - Individual failures don't affect other downloads
    """
    start_time = time.time()

    # Empty request → empty 0/0/0 response
    if len(request.urls) == 0:
        return YoutubeBatchResponse(
            success=False,
            results=[],
            total=0,
            successful=0,
            failed=0,
            processing_time=0,
        )

    # Over-limit → fail-all envelope
    if len(request.urls) > config.MAX_BATCH_URLS:
        return YoutubeBatchResponse(
            success=False,
            results=[
                YoutubeBatchItemResult(
                    url=url,
                    success=False,
                    error=f"Batch request exceeds maximum of {config.MAX_BATCH_URLS} URLs",
                )
                for url in request.urls
            ],
            total=len(request.urls),
            successful=0,
            failed=len(request.urls),
            processing_time=time.time() - start_time,
        )

    # De-duplicate while preserving order
    unique_urls = list(dict.fromkeys(request.urls))

    logger.info(
        "[Batch] Starting batch download of %d URLs (max %d concurrent)",
        len(unique_urls),
        config.MAX_CONCURRENT_DOWNLOADS,
    )

    loop = asyncio.get_event_loop()

    # LOCAL ThreadPoolExecutor — context-managed so it shuts down with the request.
    # (Module-level pool would be merge.py's job; not ours.)
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_DOWNLOADS) as executor:
        futures = [
            loop.run_in_executor(executor, download_single_youtube_mp3, url)
            for url in unique_urls
        ]
        results = await asyncio.gather(*futures, return_exceptions=True)

    # Convert any straggler exceptions into BatchItemResult error envelopes
    final_results: list[YoutubeBatchItemResult] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            final_results.append(
                YoutubeBatchItemResult(
                    url=unique_urls[i],
                    success=False,
                    error=str(result),
                )
            )
        else:
            final_results.append(result)

    successful_count = sum(1 for r in final_results if r.success)
    failed_count = len(final_results) - successful_count
    processing_time = time.time() - start_time

    logger.info(
        "[Batch] Completed: %d/%d successful in %.2fs",
        successful_count,
        len(final_results),
        processing_time,
    )

    return YoutubeBatchResponse(
        success=successful_count > 0,  # batch ok if at least one success
        results=final_results,
        total=len(final_results),
        successful=successful_count,
        failed=failed_count,
        processing_time=processing_time,
    )
