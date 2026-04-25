#!/usr/bin/env python3
"""
Mantra Creative Agent — Phase 1 smoke test.

Assumes the agent is ALREADY running on http://127.0.0.1:5555.
Start it in another terminal first:

    python main.py

Then run the smoke test:

    python tests/test_smoke.py        # works
    python -m tests.test_smoke        # also works

Exits 0 on full pass, 1 on any failure. Verbose, friendly output for debugging.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time
import traceback
from pathlib import Path

# ── Path bootstrap so this file runs both ways ────────────────────────────────
# `python tests/test_smoke.py` → CWD is agent/, but tests/ is not on sys.path
# `python -m tests.test_smoke`  → tests/ is a package, agent/ on sys.path
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

# Allow up to 60s for the first remove-bg call (u2net.onnx ~168MB download)
HEALTH_TIMEOUT = 5.0
REMOVE_BG_TIMEOUT = 60.0


# ── Helpers ───────────────────────────────────────────────────────────────────
def _print_header(label: str) -> None:
    bar = "─" * 60
    print(f"\n{bar}\n  {label}\n{bar}")


def _print_pass(label: str, detail: str = "") -> None:
    msg = f"  PASS: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _fail(test_name: str, reason: str) -> None:
    """Raise a clear failure that the main() handler will catch and report."""
    raise AssertionError(f"{test_name}: {reason}")


def _make_test_png() -> bytes:
    """Generate a 256x256 PNG with a colored circle on white background."""
    img = Image.new("RGB", (256, 256), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Solid coral circle, contrast against white BG so rembg has clear subject
    draw.ellipse((48, 48, 208, 208), fill=(255, 99, 71))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _validate_png_with_alpha(path: Path) -> tuple[int, int]:
    """Open the saved PNG, assert it has alpha channel, return (w, h)."""
    with Image.open(path) as out_img:
        out_img.load()
        if out_img.format != "PNG":
            _fail("output validation", f"expected PNG, got {out_img.format}")
        if out_img.mode not in ("RGBA", "LA"):
            _fail(
                "output validation",
                f"expected alpha channel (RGBA/LA), got mode={out_img.mode}",
            )
        return out_img.size


# ── Tests ─────────────────────────────────────────────────────────────────────
def test_health() -> None:
    _print_header("Test A — GET /api/health")

    try:
        r = httpx.get(f"{BASE_URL}/api/health", timeout=HEALTH_TIMEOUT)
    except httpx.ConnectError:
        _fail(
            "test_health",
            f"could not connect to {BASE_URL}. "
            "Is the agent running? Try `python main.py` in another terminal first.",
        )
    except httpx.TimeoutException:
        _fail("test_health", f"timed out after {HEALTH_TIMEOUT}s contacting {BASE_URL}")

    if r.status_code != 200:
        _fail("test_health", f"expected 200, got {r.status_code} — body: {r.text[:200]}")

    try:
        data = r.json()
    except Exception as e:
        _fail("test_health", f"response was not JSON: {e} — body: {r.text[:200]}")

    required_keys = {"status", "is_local_agent", "agent_version", "rembg_available", "mode"}
    missing = required_keys - set(data.keys())
    if missing:
        _fail("test_health", f"missing required keys: {sorted(missing)} — got: {sorted(data.keys())}")

    if data["status"] != "healthy":
        _fail("test_health", f"expected status='healthy', got {data['status']!r}")

    if data["is_local_agent"] is not True:
        _fail("test_health", f"expected is_local_agent=True, got {data['is_local_agent']!r}")

    _print_pass("status == healthy")
    _print_pass("is_local_agent == True")
    print(f"    agent_version  : {data.get('agent_version')}")
    print(f"    mode           : {data.get('mode')}  (GPU/CPU)")
    print(f"    rembg_available: {data.get('rembg_available')}")


def test_remove_bg_json() -> None:
    _print_header("Test B — POST /api/remove-bg (JSON base64)")

    png_bytes = _make_test_png()
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {"image": b64, "model": "u2net"}

    print(f"  → sending {len(png_bytes)} bytes (256x256 PNG, coral circle on white)")
    print(f"  → timeout {REMOVE_BG_TIMEOUT}s (first call may download u2net.onnx ~168MB)")

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/remove-bg",
            json=payload,
            timeout=REMOVE_BG_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_remove_bg_json", f"connection refused at {BASE_URL} — agent not running?")
    except httpx.TimeoutException:
        _fail(
            "test_remove_bg_json",
            f"timed out after {REMOVE_BG_TIMEOUT}s. "
            "If this is the first call, the model download may need more time.",
        )

    if r.status_code != 200:
        _fail("test_remove_bg_json", f"expected 200, got {r.status_code} — body: {r.text[:300]}")

    try:
        data = r.json()
    except Exception as e:
        _fail("test_remove_bg_json", f"response was not JSON: {e}")

    if data.get("success") is not True:
        _fail("test_remove_bg_json", f"expected success=True, got {data.get('success')!r} — error: {data.get('error')!r}")

    result_b64 = data.get("result")
    if not result_b64:
        _fail("test_remove_bg_json", "response missing `result` (base64 PNG)")

    try:
        out_bytes = base64.b64decode(result_b64)
    except Exception as e:
        _fail("test_remove_bg_json", f"could not base64-decode result: {e}")

    out_path = OUTPUT_DIR / "smoke_test_result.png"
    out_path.write_bytes(out_bytes)

    if not out_path.exists():
        _fail("test_remove_bg_json", f"output file not written to {out_path}")

    size = out_path.stat().st_size
    if size < 1024:
        _fail("test_remove_bg_json", f"output suspiciously small: {size} bytes (<1KB)")

    w, h = _validate_png_with_alpha(out_path)

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"output saved → {out_path} ({size:,} bytes)")
    _print_pass(f"valid PNG with alpha — {w}x{h}")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client round-trip)")


def test_remove_bg_file() -> None:
    _print_header("Test C — POST /api/remove-bg/file (multipart upload)")

    png_bytes = _make_test_png()
    files = {"file": ("smoke.png", png_bytes, "image/png")}

    try:
        t0 = time.perf_counter()
        r = httpx.post(
            f"{BASE_URL}/api/remove-bg/file",
            files=files,
            timeout=REMOVE_BG_TIMEOUT,
        )
        wall_time = time.perf_counter() - t0
    except httpx.ConnectError:
        _fail("test_remove_bg_file", f"connection refused at {BASE_URL}")
    except httpx.TimeoutException:
        _fail("test_remove_bg_file", f"timed out after {REMOVE_BG_TIMEOUT}s")

    if r.status_code != 200:
        _fail("test_remove_bg_file", f"expected 200, got {r.status_code} — body: {r.text[:300]}")

    try:
        data = r.json()
    except Exception as e:
        _fail("test_remove_bg_file", f"response was not JSON: {e}")

    if data.get("success") is not True:
        _fail("test_remove_bg_file", f"expected success=True, got {data.get('success')!r} — error: {data.get('error')!r}")

    result_b64 = data.get("result")
    if not result_b64:
        _fail("test_remove_bg_file", "response missing `result` (base64 PNG)")

    try:
        out_bytes = base64.b64decode(result_b64)
    except Exception as e:
        _fail("test_remove_bg_file", f"could not base64-decode result: {e}")

    out_path = OUTPUT_DIR / "smoke_test_result_file.png"
    out_path.write_bytes(out_bytes)

    if not out_path.exists():
        _fail("test_remove_bg_file", f"output file not written to {out_path}")

    size = out_path.stat().st_size
    if size < 1024:
        _fail("test_remove_bg_file", f"output suspiciously small: {size} bytes (<1KB)")

    w, h = _validate_png_with_alpha(out_path)

    _print_pass("status == 200")
    _print_pass("success == True")
    _print_pass(f"output saved → {out_path} ({size:,} bytes)")
    _print_pass(f"valid PNG with alpha — {w}x{h}")
    print(f"    processing_time : {data.get('processing_time')}s (server)")
    print(f"    wall time       : {wall_time:.2f}s (client round-trip)")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    print("\n" + "=" * 60)
    print("  Mantra Creative Agent — Smoke Test")
    print(f"  Target: {BASE_URL}")
    print(f"  Output: {OUTPUT_DIR}")
    print("=" * 60)

    overall_t0 = time.perf_counter()
    current_test = "<startup>"

    try:
        current_test = "test_health"
        test_health()

        current_test = "test_remove_bg_json"
        test_remove_bg_json()

        current_test = "test_remove_bg_file"
        test_remove_bg_file()

    except AssertionError as e:
        print(f"\nFAILED: {e}")
        elapsed = time.perf_counter() - overall_t0
        print(f"\nSmoke test FAILED after {elapsed:.1f}s")
        return 1
    except Exception as e:
        print(f"\nFAILED: {current_test}: unexpected error: {type(e).__name__}: {e}")
        traceback.print_exc()
        elapsed = time.perf_counter() - overall_t0
        print(f"\nSmoke test FAILED after {elapsed:.1f}s")
        return 1

    elapsed = time.perf_counter() - overall_t0
    print("\n" + "=" * 60)
    print(f"  All smoke tests passed in {elapsed:.1f}s")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
