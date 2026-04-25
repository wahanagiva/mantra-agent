"""
Mantra Creative Agent — /api/remove-bg handlers.

Mirrors VPS gpu_api_server.py:
  * Lines 94-100   — get_rembg_session lazy loader
  * Lines 636-670  — POST /api/remove-bg (JSON base64)
  * Lines 723-749  — POST /api/remove-bg/file (multipart upload)

LOCAL improvements over VPS:
  * Provider selection respects config.FORCE_CPU — uses CUDAExecutionProvider
    (with CPU fall-through) when GPU is enabled, vs VPS which is CPU-only.
  * Thread-safe singleton init (double-checked locking) — VPS used a plain
    global which is fine on single-worker uvicorn but the lock is cheap insurance.
  * Both endpoints share `_process_image_bytes` to eliminate duplication.
  * `model` parameter is now honored against an allow-list (M2 fix) — sessions
    are cached per model name so switching models doesn't reload u2net.

Behavior preserved:
  * Same processing pipeline: bytes → PIL → remove() → PNG → base64.
  * Same error envelope: ProcessResponse(success=False, error=str(e)).
"""
import base64
import io
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from PIL import Image

import config
from models.schemas import ProcessResponse, RemoveBgRequest
from utils.errors import format_for_response

router = APIRouter()
logger = logging.getLogger("mantra.agent.remove_bg")

# ── Allow-list of supported rembg models ─────────────────────────────────────
# Names match rembg.sessions registry (verified against
# .venv/Lib/site-packages/rembg/sessions/__init__.py). Keep this conservative —
# every entry triggers a separate ONNX download on first use.
SUPPORTED_MODELS = frozenset({
    "u2net",
    "u2net_human_seg",
    "u2netp",
    "silueta",
    "isnet-general-use",
})
DEFAULT_MODEL = "u2net"

# ── Per-model session cache (lazy, thread-safe) ──────────────────────────────
_sessions: dict = {}
_session_lock = threading.Lock()


def _get_session(model_name: str):
    """
    Lazy-load rembg session per model — cached, double-checked locking.

    Provider selection:
      * config.FORCE_CPU=True  → REMBG_PROVIDERS_CPU (CPU only)
      * config.FORCE_CPU=False → REMBG_PROVIDERS_GPU (try CUDA, fall back CPU)

    Caller MUST pass a model name already validated against SUPPORTED_MODELS.
    """
    cached = _sessions.get(model_name)
    if cached is not None:
        return cached
    with _session_lock:
        cached = _sessions.get(model_name)
        if cached is not None:
            return cached
        from rembg import new_session
        providers = (
            config.REMBG_PROVIDERS_CPU
            if config.FORCE_CPU
            else config.REMBG_PROVIDERS_GPU
        )
        logger.info("Loading rembg session model=%s providers=%s...", model_name, providers)
        session = new_session(model_name, providers=providers)
        _sessions[model_name] = session
        logger.info("rembg session loaded: %s", model_name)
        return session


def _resolve_model(requested: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Validate `requested` against SUPPORTED_MODELS.

    Returns (model_name, error_message). Exactly one is non-None:
      * (name, None)   — validated, use this name
      * (None, msg)    — caller should return error envelope with msg
    """
    if requested is None or requested == "":
        return DEFAULT_MODEL, None
    if requested in SUPPORTED_MODELS:
        return requested, None
    supported = ", ".join(sorted(SUPPORTED_MODELS))
    return None, f"Unknown model '{requested}'. Supported: {supported}"


def _process_image_bytes(image_bytes: bytes, model_name: str) -> ProcessResponse:
    """
    Core processing pipeline shared by both endpoints.

    Bytes → PIL Image → rembg.remove() → PNG buffer → base64 string.
    Returns a ProcessResponse with timing and either result or error.
    """
    start_time = time.time()
    try:
        from rembg import remove

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        logger.info("Processing remove-bg: %dx%dpx model=%s", w, h, model_name)

        session = _get_session(model_name)
        result = remove(img, session=session)

        buffer = io.BytesIO()
        result.save(buffer, format="PNG")
        result_base64 = base64.b64encode(buffer.getvalue()).decode()

        processing_time = time.time() - start_time
        logger.info("Remove-bg completed in %.2fs (model=%s)", processing_time, model_name)

        return ProcessResponse(
            success=True,
            result=result_base64,
            processing_time=processing_time,
        )

    except Exception as e:
        logger.exception("Remove-bg error")
        return ProcessResponse(success=False, error=format_for_response(e))


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/api/remove-bg", response_model=ProcessResponse)
async def remove_background(request: RemoveBgRequest):
    """
    JSON endpoint — base64 image in `request.image`, optional `request.model`.

    Mirrors VPS gpu_api_server.py:636-670 but honors the `model` field against
    SUPPORTED_MODELS (M2 fix) and uses validate=True on base64 decode (L1 fix)
    so non-base64 input fails with a clear error before reaching PIL.
    """
    model_name, err = _resolve_model(request.model)
    if err:
        return ProcessResponse(success=False, error=err)

    try:
        # validate=True rejects non-alphabet chars instead of silently stripping
        # them — guarantees garbage in → "Invalid base64" out, not a confusing
        # PIL "cannot identify image file" downstream.
        image_bytes = base64.b64decode(request.image, validate=True)
    except Exception as e:
        logger.warning("Base64 decode failed: %s", e)
        return ProcessResponse(success=False, error=f"Invalid base64 image: {format_for_response(e)}")

    return _process_image_bytes(image_bytes, model_name)


@router.post("/api/remove-bg/file", response_model=ProcessResponse)
async def remove_background_file(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
):
    """
    Multipart endpoint — raw file upload, optional `model` form field.
    Mirrors VPS gpu_api_server.py:723-749 with M2 fix for model validation.
    """
    model_name, err = _resolve_model(model)
    if err:
        return ProcessResponse(success=False, error=err)

    contents = await file.read()
    return _process_image_bytes(contents, model_name)
