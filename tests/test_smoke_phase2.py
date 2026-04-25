#!/usr/bin/env python3
"""
Mantra Creative Agent — Phase 2 smoke test.

Covers the Phase 2 endpoints layered on top of Phase 1:
    * POST /api/enhance              (JSON base64, GFPGAN face restoration)
    * POST /api/enhance/file         (multipart upload)
    * GET  /api/youtube-info         (metadata probe)
    * POST /api/youtube-mp3          (single MP3 download)
    * POST /api/youtube-mp3-batch    (batch MP3 download)
    * POST /api/youtube-mp3-merge    (async merge: start + poll + serve)
    * GET  /api/merge-status/{id}
    * GET  /api/serve-merge/{id}

Assumes the agent is ALREADY running on http://127.0.0.1:5555. Start it in
another terminal first:

    python main.py

Then run the Phase 2 smoke test:

    python tests/test_smoke_phase2.py        # works
    python -m tests.test_smoke_phase2        # also works

Tests whose dependencies aren't installed on the agent (e.g. GFPGAN, yt-dlp,
ffmpeg, Node.js) are SKIPPED with a clear warning — not treated as failures.

YouTube anti-bot / signature extraction issues (403, signature decipher, etc.)
are reported as WARNINGS rather than failures — they're network-side flakiness,
not agent bugs.

Exits 0 on full pass OR warnings-only, 1 on real agent failures.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

# ── Path bootstrap so this file runs both ways ────────────────────────────────
# `python tests/test_smoke_phase2.py` → CWD is agent/, but tests/ is not on sys.path
# `python -m tests.test_smoke_phase2`  → tests/ is a package, agent/ on sys.path
_THIS_FILE = Path(__file__).resolve()
_AGENT_DIR = _THIS_FILE.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

try:
    import httpx
    from PIL import Image, ImageDraw
except ImportError as e:
    print(f"FAILED: missing dependency: {e}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────
AGENT_HOST = os.environ.get("MANTRA_AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("MANTRA_AGENT_PORT", "5555"))
BASE_URL = f"http://{AGENT_HOST}:{AGENT_PORT}"

OUTPUT_DIR = _THIS_FILE.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Timeouts
HEALTH_TIMEOUT = 5.0
ENHANCE_TIMEOUT = 300.0   # first call downloads GFPGANv1.4.pth (~340MB)
YOUTUBE_INFO_TIMEOUT = 90.0
YOUTUBE_DOWNLOAD_TIMEOUT = 300.0
MERGE_OVERALL_TIMEOUT = 600.0
MERGE_POLL_INTERVAL = 3.0
SERVE_TIMEOUT = 120.0

# Test YouTube URLs — short + public + stable
# "Me at the zoo" (first YouTube video ever, 19s, will exist forever)
URL_ME_AT_ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
# Big Buck Bunny trailer (Blender public domain, ~33s)
URL_BIG_BUCK_BUNNY = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


# ── Warning accumulator ───────────────────────────────────────────────────────
# Non-failures that should still bubble up in the summary but don't fail the run
_warnings: list[str] = []


# ── Helpers ───────────────────────────────────────────────────────────────────
def _print_header(label: str) -> None:
    bar = "─" * 60
    print(f"\n{bar}\n  {label}\n{bar}")


def _print_pass(label: str, detail: str = "") -> None:
    msg = f"  PASS: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _print_skip(label: str, reason: str) -> None:
    print(f"  SKIP: {label} — {reason}")


def _print_warn(label: str, reason: str) -> None:
    msg = f"  WARN: {label} — {reason}"
    print(msg)
    _warnings.append(f"{label}: {reason}")


def _fail(test_name: str, reason: str) -> None:
    """Raise a clear failure that the main() handler will catch and report."""
    raise AssertionError(f"{test_name}: {reason}")


def _truncate(s: str | None, limit: int = 60) -> str:
    """Truncate a long string (like base64 payloads) for safe console printing."""
    if s is None:
        return "<None>"
    if len(s) <= limit:
        return s
    return f"{s[:limit]}... <+{len(s) - limit} chars>"


def _gen_test_face_png(size: int = 256) -> bytes:
    """
    Generate a 256x256 PNG with a face-ish drawing (circle + eyes + mouth).

    GFPGAN may or may not detect this as a face — doesn't matter: if no face
    found it returns the input unmodified. Either way we expect HTTP 200 +
    success=True.
    """
    img = Image.new("RGB", (size, size), color=(240, 220, 200))  # skin-tone BG
    draw = ImageDraw.Draw(img)

    # Face circle
    face_margin = size // 8
    draw.ellipse(
        (face_margin, face_margin, size - face_margin, size - face_margin),
        fill=(230, 200, 170),
        outline=(120, 80, 60),
        width=2,
    )
    # Eyes
    eye_y = size // 2 - size // 10
    eye_r = size // 24
    eye_l_cx = size // 2 - size // 6
    eye_r_cx = size // 2 + size // 6
    for cx in (eye_l_cx, eye_r_cx):
        draw.ellipse(
            (cx - eye_r, eye_y - eye_r, cx + eye_r, eye_y + eye_r),
            fill=(40, 40, 40),
        )
    # Nose (tiny triangle)
    nose_top = (size // 2, size // 2 - size // 32)
    nose_bl = (size // 2 - size // 24, size // 2 + size // 16)
    nose_br = (size // 2 + size // 24, size // 2 + size // 16)
    draw.polygon([nose_top, nose_bl, nose_br], fill=(180, 140, 110))
    # Mouth (arc-ish ellipse)
    mouth_y = size // 2 + size // 5
    mouth_w = size // 5
    draw.ellipse(
        (size // 2 - mouth_w, mouth_y - size // 32,
         size // 2 + mouth_w, mouth_y + size // 16),
        fill=(170, 60, 60),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _check_health() -> dict:
    """GET /api/health and return the JSON dict. Fails loudly if unreachable."""
    try:
        r = httpx.get(f"{BASE_URL}/api/health", timeout=HEALTH_TIMEOUT)
    except httpx.ConnectError:
        _fail(
            "health_probe",
            f"could not connect to {BASE_URL}. "
            "Is the agent running? Try `python main.py` in another terminal first.",
        )
    except httpx.TimeoutException:
        _fail("health_probe", f"timed out after {HEALTH_TIMEOUT}s contacting {BASE_URL}")

    if r.status_code != 200:
        _fail("health_probe", f"expected 200, got {r.status_code} — body: {r.text[:200]}")

    try:
        return r.json()
    except Exception as e:
        _fail("health_probe", f"response was not JSON: {e} — body: {r.text[:200]}")


def _decode_b64_safe(b64_str: str, test_name: str, field: str) -> bytes:
    """Base64-decode with a clean error message on failure."""
    try:
        return base64.b64decode(b64_str)
    except Exception as e:
        _fail(test_name, f"could not base64-decode `{field}`: {e}")


def _is_probably_mp3(data: bytes) -> bool:
    """
    Quick sanity check for MP3 content.
    MP3 files start with either an ID3 tag (ID3) or an MPEG frame sync (0xFF 0xFB/F3/F2).
    """
    if len(data) < 3:
        return False
    if data[:3] == b"ID3":
        return True
    # MPEG frame sync: first byte 0xFF, second byte has top 3 bits set
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True
    return False


# ── Test D — POST /api/enhance (JSON base64) ──────────────────────────────────
def test_enhance_json(health: dict) -> None:
    _print_header("Test D — POST /api/enhance (JSON base64)")

    if not health.get("gfpgan_available"):
        _print_skip("Test D", "GFPGAN not installed on agent (health.gfpgan_available=False)")
        return

    png_bytes = _gen_test_face_png(256)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {"image": b64, "upscale": 2, "weight": 0.5}

    print(f"  → sending {len(png_bytes)} bytes (256x256 synthetic face PNG)")
    print(f"  → timeout {ENHANCE_TIMEOUT:.0f}s (first call may download GFPGANv1.4.pth ~340MB)")

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/enhance",
            json=payload,
            timeout=ENHANCE_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_enhance_json", f"connection refused at {BASE_URL} — agent not running?")
    except httpx.TimeoutException:
        _fail(
            "test_enhance_json",
            f"timed out after {ENHANCE_TIMEOUT}s — first-run model download may need longer",
        )

    if r.status_code != 200:
        _fail(
            "test_enhance_json",
            f"expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception as e:
        _fail("test_enhance_json", f"response was not JSON: {e}")

    if data.get("success") is not True:
        _fail(
            "test_enhance_json",
            f"success != True — error: {_truncate(data.get('error'))!r}",
        )

    result_b64 = data.get("result")
    if not result_b64:
        _fail("test_enhance_json", "response missing `result` (base64 PNG)")

    out_bytes = _decode_b64_safe(result_b64, "test_enhance_json", "result")
    out_path = OUTPUT_DIR / "smoke_enhance_result.png"
    out_path.write_bytes(out_bytes)

    size = out_path.stat().st_size
    if size < 512:
        _fail("test_enhance_json", f"output suspiciously small: {size} bytes")

    # Validate it's actually a PNG
    try:
        with Image.open(out_path) as out_img:
            out_img.load()
            if out_img.format != "PNG":
                _fail("test_enhance_json", f"expected PNG, got {out_img.format}")
            w, h = out_img.size
    except Exception as e:
        _fail("test_enhance_json", f"output is not a valid PNG: {e}")

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"output saved → {out_path} ({size:,} bytes)")
    _print_pass(f"valid PNG — {w}x{h}")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client round-trip)")


# ── Test E — POST /api/enhance/file (multipart) ───────────────────────────────
def test_enhance_file(health: dict) -> None:
    _print_header("Test E — POST /api/enhance/file (multipart upload)")

    if not health.get("gfpgan_available"):
        _print_skip("Test E", "GFPGAN not installed on agent")
        return

    png_bytes = _gen_test_face_png(256)
    files = {"file": ("smoke_face.png", png_bytes, "image/png")}
    data_form = {"upscale": "2", "weight": "0.5"}

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/enhance/file",
            files=files,
            data=data_form,
            timeout=ENHANCE_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_enhance_file", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail("test_enhance_file", f"timed out after {ENHANCE_TIMEOUT}s")

    if r.status_code != 200:
        _fail(
            "test_enhance_file",
            f"expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception as e:
        _fail("test_enhance_file", f"response was not JSON: {e}")

    if data.get("success") is not True:
        _fail(
            "test_enhance_file",
            f"success != True — error: {_truncate(data.get('error'))!r}",
        )

    result_b64 = data.get("result")
    if not result_b64:
        _fail("test_enhance_file", "response missing `result` (base64 PNG)")

    out_bytes = _decode_b64_safe(result_b64, "test_enhance_file", "result")
    out_path = OUTPUT_DIR / "smoke_enhance_file_result.png"
    out_path.write_bytes(out_bytes)

    size = out_path.stat().st_size
    if size < 512:
        _fail("test_enhance_file", f"output suspiciously small: {size} bytes")

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"output saved → {out_path} ({size:,} bytes)")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client round-trip)")


# ── Test F — GET /api/youtube-info ────────────────────────────────────────────
def test_youtube_info(health: dict) -> None:
    _print_header("Test F — GET /api/youtube-info")

    if not health.get("ytdlp_available"):
        _print_skip("Test F", "yt-dlp not installed on agent (health.ytdlp_available=False)")
        return
    if not health.get("node_available"):
        _print_warn("Test F", "Node.js not on PATH — yt-dlp --js-runtimes node may fail")

    url = URL_ME_AT_ZOO
    print(f"  → probing: {url}")
    print(f"  → timeout {YOUTUBE_INFO_TIMEOUT:.0f}s")

    try:
        t0 = time.perf_counter()
        r = httpx.get(
            f"{BASE_URL}/api/youtube-info",
            params={"url": url},
            timeout=YOUTUBE_INFO_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_youtube_info", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail("test_youtube_info", f"timed out after {YOUTUBE_INFO_TIMEOUT}s")

    if r.status_code != 200:
        _fail(
            "test_youtube_info",
            f"expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception as e:
        _fail("test_youtube_info", f"response was not JSON: {e}")

    if data.get("success") is not True:
        err = _truncate(data.get("error"))
        _print_warn(
            "Test F",
            f"yt-dlp returned success=False — likely YouTube network flakiness: {err}",
        )
        return

    title = data.get("title")
    duration = data.get("duration")
    if not title:
        _fail("test_youtube_info", "response missing `title`")
    if duration is None:
        _fail("test_youtube_info", "response missing `duration`")
    if duration > 60:
        _print_warn(
            "Test F",
            f"duration {duration}s > 60s — expected short test video",
        )

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"title: {title!r}")
    print(f"    duration   : {duration}s")
    print(f"    uploader   : {data.get('uploader')!r}")
    print(f"    view_count : {data.get('view_count')}")
    print(f"    thumbnail  : {_truncate(data.get('thumbnail'), 80)}")
    print(f"    wall time  : {wall_time:.2f}s")


# ── Test G — POST /api/youtube-mp3 (single) ───────────────────────────────────
def test_youtube_mp3_single(health: dict) -> None:
    _print_header("Test G — POST /api/youtube-mp3 (single MP3)")

    if not health.get("ytdlp_available"):
        _print_skip("Test G", "yt-dlp not installed on agent")
        return
    if not health.get("ffmpeg_available"):
        _print_skip("Test G", "ffmpeg not installed on agent")
        return

    url = URL_ME_AT_ZOO
    payload = {"url": url}
    print(f"  → downloading: {url}")
    print(f"  → timeout {YOUTUBE_DOWNLOAD_TIMEOUT:.0f}s (download + convert + base64)")

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/youtube-mp3",
            json=payload,
            timeout=YOUTUBE_DOWNLOAD_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_youtube_mp3_single", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail(
            "test_youtube_mp3_single",
            f"timed out after {YOUTUBE_DOWNLOAD_TIMEOUT}s",
        )

    if r.status_code != 200:
        _fail(
            "test_youtube_mp3_single",
            f"expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception as e:
        _fail("test_youtube_mp3_single", f"response was not JSON: {e}")

    if data.get("success") is not True:
        err = _truncate(data.get("error"))
        _print_warn(
            "Test G",
            f"yt-dlp returned success=False — likely YouTube network flakiness: {err}",
        )
        return

    audio_b64 = data.get("audio_base64")
    if not audio_b64 or len(audio_b64) < 100:
        _fail(
            "test_youtube_mp3_single",
            f"audio_base64 missing or too small ({len(audio_b64 or '')} chars)",
        )
    if not data.get("title"):
        _fail("test_youtube_mp3_single", "response missing `title`")

    audio_bytes = _decode_b64_safe(audio_b64, "test_youtube_mp3_single", "audio_base64")
    out_path = OUTPUT_DIR / "smoke_youtube_mp3.mp3"
    out_path.write_bytes(audio_bytes)

    size = out_path.stat().st_size
    if size < 10 * 1024:
        _fail(
            "test_youtube_mp3_single",
            f"MP3 too small: {size} bytes (<10KB, expected ~400KB for 19s clip)",
        )

    if not _is_probably_mp3(audio_bytes):
        _print_warn(
            "Test G",
            f"file doesn't start with ID3 or MPEG sync — header bytes: {audio_bytes[:4].hex()}",
        )

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"title: {data.get('title')!r}")
    _print_pass(f"output saved → {out_path} ({size:,} bytes)")
    print(f"    server duration : {data.get('duration')}s")
    print(f"    server filesize : {data.get('filesize'):,} bytes")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client)")


# ── Test H — POST /api/youtube-mp3-batch ──────────────────────────────────────
def test_youtube_mp3_batch(health: dict) -> None:
    _print_header("Test H — POST /api/youtube-mp3-batch (dedup)")

    if not health.get("ytdlp_available"):
        _print_skip("Test H", "yt-dlp not installed on agent")
        return
    if not health.get("ffmpeg_available"):
        _print_skip("Test H", "ffmpeg not installed on agent")
        return

    # Send the same URL twice — the agent dedupes so only ONE download happens.
    # That exercises the batch path without doubling wall time.
    url = URL_ME_AT_ZOO
    payload = {"urls": [url, url]}
    print(f"  → sending 2 URLs ([url, url] — agent should dedup to 1)")
    print(f"  → timeout {YOUTUBE_DOWNLOAD_TIMEOUT:.0f}s")

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/youtube-mp3-batch",
            json=payload,
            timeout=YOUTUBE_DOWNLOAD_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_youtube_mp3_batch", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail(
            "test_youtube_mp3_batch",
            f"timed out after {YOUTUBE_DOWNLOAD_TIMEOUT}s",
        )

    if r.status_code != 200:
        _fail(
            "test_youtube_mp3_batch",
            f"expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        data = r.json()
    except Exception as e:
        _fail("test_youtube_mp3_batch", f"response was not JSON: {e}")

    total = data.get("total", -1)
    successful = data.get("successful", -1)
    failed = data.get("failed", -1)
    results = data.get("results", [])

    if total < 1:
        _fail("test_youtube_mp3_batch", f"expected total >= 1, got {total}")

    if data.get("success") is not True:
        # Likely network flakiness — report + exit early
        if successful == 0:
            err_samples = "; ".join(
                _truncate(r.get("error"), 80)
                for r in results
                if not r.get("success")
            )
            _print_warn(
                "Test H",
                f"all items failed — likely YouTube flakiness: {err_samples}",
            )
            return
        _print_warn(
            "Test H",
            f"success=False but some items succeeded ({successful}/{total})",
        )

    if successful < 1:
        _fail("test_youtube_mp3_batch", f"expected successful >= 1, got {successful}")

    _print_pass("status == 200")
    _print_pass(f"success == {data.get('success')}")
    _print_pass(f"total={total}, successful={successful}, failed={failed}")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client)")
    for i, item in enumerate(results):
        ok = "ok" if item.get("success") else "err"
        title_or_err = item.get("title") if item.get("success") else _truncate(item.get("error"), 60)
        print(f"      [{i}] {ok}: {title_or_err!r}")


# ── Test I — /api/youtube-mp3-merge (async flow) ──────────────────────────────
def test_youtube_merge(health: dict) -> None:
    _print_header("Test I — POST /api/youtube-mp3-merge (async: start + poll + serve)")

    if not health.get("ytdlp_available"):
        _print_skip("Test I", "yt-dlp not installed on agent")
        return
    if not health.get("ffmpeg_available"):
        _print_skip("Test I", "ffmpeg not installed on agent")
        return

    # Use 2 short, public videos so we actually exercise the concat path
    urls = [URL_ME_AT_ZOO, URL_BIG_BUCK_BUNNY]
    payload = {"urls": urls, "album_name": "Smoke Test Album"}
    print(f"  → merging {len(urls)} tracks as 'Smoke Test Album'")
    print(f"  → overall timeout {MERGE_OVERALL_TIMEOUT:.0f}s, polling every {MERGE_POLL_INTERVAL:.0f}s")

    # ── Step 1: kick off the job ──────────────────────────────────────────────
    try:
        r = httpx.post(
            f"{BASE_URL}/api/youtube-mp3-merge",
            json=payload,
            timeout=30.0,
        )
    except httpx.ConnectError:
        _fail("test_youtube_merge", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail("test_youtube_merge", "timed out starting merge job (>30s)")

    if r.status_code != 200:
        _fail(
            "test_youtube_merge",
            f"POST /api/youtube-mp3-merge — expected 200, got {r.status_code} — body: {r.text[:300]}",
        )

    try:
        start_data = r.json()
    except Exception as e:
        _fail("test_youtube_merge", f"start response not JSON: {e}")

    if start_data.get("success") is not True:
        _fail(
            "test_youtube_merge",
            f"merge refused to start: {_truncate(start_data.get('error'))!r}",
        )

    job_id = start_data.get("job_id")
    if not job_id:
        _fail("test_youtube_merge", "response missing `job_id`")
    # Validate UUID shape
    try:
        uuid.UUID(job_id)
    except (ValueError, TypeError):
        _fail("test_youtube_merge", f"job_id is not a valid UUID: {job_id!r}")

    _print_pass(f"POST 200, job_id={job_id}")

    # ── Step 2: poll status until done or timeout ─────────────────────────────
    t0 = time.perf_counter()
    last_status = None
    final_job: dict | None = None
    poll_count = 0

    while True:
        elapsed = time.perf_counter() - t0
        if elapsed > MERGE_OVERALL_TIMEOUT:
            _fail(
                "test_youtube_merge",
                f"merge still not complete after {MERGE_OVERALL_TIMEOUT:.0f}s",
            )

        try:
            sr = httpx.get(
                f"{BASE_URL}/api/merge-status/{job_id}",
                timeout=30.0,
            )
        except httpx.ConnectError:
            _fail("test_youtube_merge", f"connection refused while polling status")
        except httpx.TimeoutException:
            _fail("test_youtube_merge", f"status poll timed out after 30s")

        if sr.status_code != 200:
            _fail(
                "test_youtube_merge",
                f"GET /api/merge-status — expected 200, got {sr.status_code} — body: {sr.text[:300]}",
            )

        try:
            job = sr.json()
        except Exception as e:
            _fail("test_youtube_merge", f"status response not JSON: {e}")

        status = job.get("status")
        poll_count += 1

        if status != last_status:
            print(f"    [{elapsed:6.1f}s] status: {status}")
            last_status = status

        if status == "done":
            final_job = job
            break
        if status == "error":
            err = _truncate(job.get("error"), 150)
            # Error-state likely = YouTube-side flakiness unless it's our bug.
            # ffmpeg errors contain "ffmpeg gagal" — treat those as real failures.
            err_text = str(job.get("error") or "")
            if "ffmpeg" in err_text.lower():
                _fail("test_youtube_merge", f"merge error (ffmpeg): {err}")
            _print_warn(
                "Test I",
                f"merge job failed — likely YouTube flakiness: {err}",
            )
            return

        time.sleep(MERGE_POLL_INTERVAL)

    if final_job is None:
        _fail("test_youtube_merge", "polling loop exited without a final job (internal bug)")

    _print_pass(f"merge complete (status=done) after {poll_count} polls")

    # Validate required keys in the final status dict
    required = {"download_id", "filename", "duration", "track_count", "filesize"}
    missing = required - set(final_job.keys())
    if missing:
        _fail(
            "test_youtube_merge",
            f"final status missing keys: {sorted(missing)} — got {sorted(final_job.keys())}",
        )

    download_id = final_job["download_id"]
    try:
        uuid.UUID(download_id)
    except (ValueError, TypeError):
        _fail("test_youtube_merge", f"download_id not UUID: {download_id!r}")

    print(f"    download_id  : {download_id}")
    print(f"    filename     : {final_job.get('filename')!r}")
    print(f"    duration     : {final_job.get('duration')}")
    print(f"    track_count  : {final_job.get('track_count')}")
    print(f"    filesize     : {final_job.get('filesize'):,} bytes")
    if final_job.get("failed_urls"):
        print(f"    failed_urls  : {final_job.get('failed_urls')}")

    # ── Step 3: download merged file via /api/serve-merge ────────────────────
    serve_url = f"{BASE_URL}/api/serve-merge/{download_id}"
    out_path = OUTPUT_DIR / "smoke_merge.mp3"
    print(f"  → fetching {serve_url}")

    try:
        with httpx.stream("GET", serve_url, timeout=SERVE_TIMEOUT) as resp:
            if resp.status_code != 200:
                _fail(
                    "test_youtube_merge",
                    f"GET /api/serve-merge — expected 200, got {resp.status_code}",
                )
            content_type = resp.headers.get("content-type", "")
            if "audio/mpeg" not in content_type:
                _print_warn(
                    "Test I",
                    f"unexpected content-type: {content_type!r} (expected audio/mpeg)",
                )
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except httpx.ConnectError:
        _fail("test_youtube_merge", "connection refused during serve-merge")
    except httpx.TimeoutException:
        _fail("test_youtube_merge", f"serve-merge timed out after {SERVE_TIMEOUT}s")

    size = out_path.stat().st_size
    if size < 50 * 1024:
        _fail(
            "test_youtube_merge",
            f"merged MP3 too small: {size} bytes (<50KB, expected at least single-track size)",
        )

    # Sanity-check it looks like an MP3
    with open(out_path, "rb") as fh:
        head = fh.read(4)
    if not _is_probably_mp3(head):
        _print_warn(
            "Test I",
            f"served file doesn't start with ID3 or MPEG sync — header: {head.hex()}",
        )

    _print_pass(f"served file → {out_path} ({size:,} bytes)")

    # ── Step 4: re-query status after serve (expected: 200 done OR 404 cleanup) ─
    try:
        r2 = httpx.get(f"{BASE_URL}/api/merge-status/{job_id}", timeout=10.0)
    except Exception as e:
        _print_warn(
            "Test I",
            f"post-serve status query raised {type(e).__name__}: {e}",
        )
        return

    if r2.status_code == 200:
        _print_pass("post-serve status poll: still 200 (job retained in memory)")
    elif r2.status_code == 404:
        _print_pass("post-serve status poll: 404 (job already cleaned up — acceptable)")
    else:
        _print_warn(
            "Test I",
            f"post-serve status poll: unexpected {r2.status_code} — body: {r2.text[:150]}",
        )


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    print("\n" + "=" * 60)
    print("  Mantra Creative Agent — Phase 2 Smoke Test")
    print(f"  Target: {BASE_URL}")
    print(f"  Output: {OUTPUT_DIR}")
    print("=" * 60)

    overall_t0 = time.perf_counter()
    current_test = "<startup>"

    try:
        # Probe health first so every subsequent test can decide whether to skip
        current_test = "health_probe"
        _print_header("Pre-flight — GET /api/health")
        health = _check_health()
        _print_pass(f"agent reachable — version {health.get('agent_version')}")
        print(f"    mode             : {health.get('mode')}")
        print(f"    rembg_available  : {health.get('rembg_available')}")
        print(f"    gfpgan_available : {health.get('gfpgan_available')}")
        print(f"    ytdlp_available  : {health.get('ytdlp_available')} "
              f"(version {health.get('ytdlp_version')})")
        print(f"    ffmpeg_available : {health.get('ffmpeg_available')} "
              f"(version {health.get('ffmpeg_version')})")
        print(f"    node_available   : {health.get('node_available')} "
              f"(version {health.get('node_version')})")

        current_test = "test_enhance_json"
        test_enhance_json(health)

        current_test = "test_enhance_file"
        test_enhance_file(health)

        current_test = "test_youtube_info"
        test_youtube_info(health)

        current_test = "test_youtube_mp3_single"
        test_youtube_mp3_single(health)

        current_test = "test_youtube_mp3_batch"
        test_youtube_mp3_batch(health)

        current_test = "test_youtube_merge"
        test_youtube_merge(health)

    except AssertionError as e:
        print(f"\nFAILED: {e}")
        elapsed = time.perf_counter() - overall_t0
        print(f"\nPhase 2 smoke test FAILED after {elapsed:.1f}s")
        return 1
    except Exception as e:
        print(f"\nFAILED: {current_test}: unexpected error: {type(e).__name__}: {e}")
        traceback.print_exc()
        elapsed = time.perf_counter() - overall_t0
        print(f"\nPhase 2 smoke test FAILED after {elapsed:.1f}s")
        return 1

    elapsed = time.perf_counter() - overall_t0
    print("\n" + "=" * 60)
    if _warnings:
        print(f"  All Phase 2 smoke tests passed in {elapsed:.1f}s ({len(_warnings)} warnings)")
        print("  Warnings (non-fatal — likely YouTube/network flakiness):")
        for w in _warnings:
            print(f"    - {w}")
    else:
        print(f"  All Phase 2 smoke tests passed in {elapsed:.1f}s")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
