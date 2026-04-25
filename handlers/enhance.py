"""
Mantra Creative Agent — /api/enhance handlers (GFPGAN face restoration).

Mirrors VPS gpu_api_server.py:
  * Lines 102-129  — get_gfpgan_restorer lazy loader (with upscale-factor cache)
  * Lines 672-721  — POST /api/enhance (JSON base64)
  * Lines 751-797  — POST /api/enhance/file (multipart upload)

LOCAL improvements over VPS:
  * SMART GPU detection — uses CUDA when available (and not forced CPU), unlike
    VPS which hardcodes "cpu" because the RTX 5060 (Blackwell sm_120) isn't yet
    supported by the shipped PyTorch wheels. User PCs with RTX 30/40-series can
    benefit from CUDA acceleration.
  * CUDA OOM auto-fallback — if GPU runs out of memory mid-inference, the
    handler retries on CPU transparently and logs a WARNING.
  * Dual-cache restorer: (upscale, device) keyed — switching between GPU and
    CPU paths doesn't redundantly reload the model.
  * Friendly error envelope via utils.errors.humanize_error — instead of raw
    traceback text the client sees actionable hints ("try smaller image", etc).

Behavior preserved:
  * Same upscale-factor cache semantics (extended with device dimension).
  * Same processing pipeline: bytes → cv2 ndarray → GFPGANer.enhance() → PNG → b64.
  * Same GFPGAN params: arch="clean", channel_multiplier=2, model v1.4 from GitHub.
"""
import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import APIRouter, File, Form, UploadFile

import config
from models.schemas import EnhanceRequest, ProcessResponse
from utils.errors import format_for_response
from utils.gpu_safe import is_cuda_oom

router = APIRouter()
logger = logging.getLogger("mantra.agent.enhance")

# ── M3/M4 guards ──────────────────────────────────────────────────────────────
# Allowed upscale factors (GFPGAN only supports these).
_ALLOWED_UPSCALES = (1, 2, 4)
# M4 — DoS guard on input/output pixel counts.
#   MAX_INPUT_PIXELS  = 4 MP   → covers up to ~2000x2000 (typical phone photos).
#   MAX_OUTPUT_PIXELS = 16 MP  → caps a 2000x2000 image at upscale=2 (4000x4000),
#                                or a ~1000x1000 image at upscale=4. Larger jobs
#                                take 30s+ on CPU — easy DoS surface.
MAX_INPUT_PIXELS = 4_000_000
MAX_OUTPUT_PIXELS = 16_000_000

# ── Dual-cache restorer: (upscale, device) → GFPGANer ─────────────────────────
_restorers: Dict[Tuple[int, str], Any] = {}
_restorer_lock = threading.Lock()


def _detect_device() -> str:
    """
    Pick "cuda" if torch+CUDA available and user hasn't forced CPU, else "cpu".
    """
    if config.FORCE_CPU:
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    except Exception:
        pass
    return "cpu"


def _get_restorer(upscale: int, device: str):
    """
    Lazy-load GFPGAN restorer keyed by (upscale, device).

    Returns a GFPGANer instance, or None if gfpgan/torch can't be imported.
    Thread-safe with double-checked locking.

    Mirrors VPS get_gfpgan_restorer (gpu_api_server.py:102-129) but supports
    multiple device caches for auto-fallback on OOM.
    """
    key = (upscale, device)
    # Fast path: cached hit, no lock contention
    if key in _restorers:
        return _restorers[key]
    with _restorer_lock:
        # Double-check after acquiring lock
        if key in _restorers:
            return _restorers[key]
        try:
            from gfpgan import GFPGANer
            import gfpgan.utils as _gu
        except ImportError:
            logger.warning("gfpgan not installed — returning None")
            return None
        except Exception as e:
            logger.error("gfpgan import failed: %s", e)
            return None

        # ── CRITICAL: redirect aux-model cache to a writable location ──────
        # GFPGAN's FaceRestoreHelper downloads face detection (ResNet50) and
        # parsing models into os.path.join(ROOT_DIR, 'gfpgan/weights'). In a
        # PyInstaller + Inno Setup install under Program Files, ROOT_DIR is
        # READ-ONLY → mkdir fails with WinError 5 "Access is denied: 'gfpgan'".
        #
        # Fix: point ROOT_DIR at APP_DATA_DIR/models (user-writable). Also
        # pre-copy the bundled GFPGANv1.4.pth so we don't re-download 340 MB
        # on first use after install.
        writable_root = config.APP_DATA_DIR / "models"
        (writable_root / "gfpgan" / "weights").mkdir(parents=True, exist_ok=True)
        try:
            bundled_gfpgan = Path(_gu.__file__).parent / "weights" / "GFPGANv1.4.pth"
            target_gfpgan = writable_root / "gfpgan" / "weights" / "GFPGANv1.4.pth"
            if bundled_gfpgan.exists() and not target_gfpgan.exists():
                import shutil as _sh
                _sh.copy2(bundled_gfpgan, target_gfpgan)
                logger.info("Pre-copied bundled GFPGANv1.4.pth → %s", target_gfpgan)
        except Exception as e:
            logger.debug("Pre-copy bundled GFPGAN model failed: %s", e)
        # Apply the ROOT_DIR override (must happen before GFPGANer() constructor)
        _gu.ROOT_DIR = str(writable_root)

        # ── ALSO patch FaceRestoreHelper (facexlib) so its model_rootpath is
        # absolute. GFPGANer hardcodes `model_rootpath='gfpgan/weights'` (RELATIVE)
        # when constructing FaceRestoreHelper, which then does
        # `os.makedirs('gfpgan/weights')` from CWD. CWD on a Program Files install
        # is read-only → PermissionError [WinError 5] "Access is denied: 'gfpgan'".
        # Override __init__ once to absolutise the path.
        try:
            import facexlib.utils.face_restoration_helper as _frh_mod
            if not getattr(_frh_mod.FaceRestoreHelper, "_mantra_patched", False):
                _orig_frh_init = _frh_mod.FaceRestoreHelper.__init__
                _writable_str = str(writable_root)
                def _patched_frh_init(self, *args, **kwargs):
                    rp = kwargs.get("model_rootpath")
                    if rp and not os.path.isabs(rp):
                        kwargs["model_rootpath"] = os.path.join(_writable_str, rp)
                    return _orig_frh_init(self, *args, **kwargs)
                _frh_mod.FaceRestoreHelper.__init__ = _patched_frh_init
                _frh_mod.FaceRestoreHelper._mantra_patched = True
                logger.info("Patched FaceRestoreHelper.__init__ to absolutise model_rootpath under %s", _writable_str)
        except Exception as e:
            logger.warning("Could not patch FaceRestoreHelper: %s", e)

        # Prefer the pre-copied local path when available, else fall back to
        # URL (which will now download to writable location).
        local_model = writable_root / "gfpgan" / "weights" / "GFPGANv1.4.pth"
        model_path = str(local_model) if local_model.exists() else \
            "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth"

        logger.info("Loading GFPGAN on %s (upscale=%d, model=%s)...",
                    device, upscale, "local" if local_model.exists() else "URL")
        restorer = GFPGANer(
            model_path=model_path,
            upscale=upscale,
            arch="clean",
            channel_multiplier=2,
            device=device,
        )
        _restorers[key] = restorer
        logger.info("GFPGAN loaded on %s", device)
        return restorer


def _run_enhance(img, upscale: int, weight: float, device: str):
    """Run GFPGAN.enhance() on the given device. Raises on failure."""
    restorer = _get_restorer(upscale, device)
    if restorer is None:
        raise RuntimeError("GFPGAN not available. Install torch and gfpgan packages.")
    _, _, output = restorer.enhance(
        img,
        has_aligned=False,
        only_center_face=False,
        paste_back=True,
        weight=weight,
    )
    return output


def _process_image_bytes(image_bytes: bytes, upscale: int, weight: float) -> ProcessResponse:
    """
    Core processing pipeline shared by both endpoints.

    Pipeline: bytes → cv2 BGR ndarray → GFPGANer.enhance() → PNG buffer → base64.
    Retries on CPU if a CUDA OOM triggers during GPU inference.
    """
    start_time = time.time()

    # ── M3 — defence-in-depth validation (Pydantic/Form already check, but
    # _process_image_bytes is also reachable from internal callers). ─────────
    if upscale not in _ALLOWED_UPSCALES:
        return ProcessResponse(
            success=False,
            error=f"Invalid upscale={upscale}. Allowed values: {list(_ALLOWED_UPSCALES)}.",
        )
    if not (0.0 <= weight <= 1.0):
        return ProcessResponse(
            success=False,
            error=f"Invalid weight={weight}. Must be in [0.0, 1.0].",
        )

    try:
        # Decode image bytes → OpenCV BGR ndarray (heavy deps imported lazily)
        import cv2
        import numpy as np

        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Failed to decode image")

        h, w = img.shape[:2]

        # ── M4 — DoS guard on input/output dimensions. Reject before invoking
        # the (expensive) GFPGAN pipeline. ─────────────────────────────────────
        in_px = w * h
        out_px = in_px * (upscale ** 2)
        if in_px > MAX_INPUT_PIXELS:
            logger.warning(
                "Reject enhance: input too large (%dx%d = %d px > %d).",
                w, h, in_px, MAX_INPUT_PIXELS,
            )
            return ProcessResponse(
                success=False,
                error=(
                    f"Input image too large ({w}x{h} = {in_px} px). "
                    f"Max input: {MAX_INPUT_PIXELS} px (~2000x2000)."
                ),
            )
        if out_px > MAX_OUTPUT_PIXELS:
            logger.warning(
                "Reject enhance: output too large (%dx%d = %d px > %d).",
                w * upscale, h * upscale, out_px, MAX_OUTPUT_PIXELS,
            )
            return ProcessResponse(
                success=False,
                error=(
                    f"Output too large ({w * upscale}x{h * upscale} = {out_px} px). "
                    f"Use a smaller image or lower upscale (max output: {MAX_OUTPUT_PIXELS} px)."
                ),
            )

        primary_device = _detect_device()
        logger.info(
            "Processing enhance: %dx%dpx, upscale=%dx, weight=%.2f, device=%s",
            w, h, upscale, weight, primary_device,
        )

        # Try primary device first; auto-fallback to CPU on CUDA OOM
        try:
            output = _run_enhance(img, upscale, weight, primary_device)
        except Exception as e:
            if primary_device == "cuda" and is_cuda_oom(e):
                logger.warning(
                    "CUDA OOM during enhance (%s) — retrying on CPU",
                    e.__class__.__name__,
                )
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                output = _run_enhance(img, upscale, weight, "cpu")
            else:
                raise

        # Re-encode to PNG → base64
        _, buffer = cv2.imencode(".png", output)
        result_base64 = base64.b64encode(buffer).decode()

        processing_time = time.time() - start_time
        logger.info("Enhance completed in %.2fs", processing_time)

        return ProcessResponse(
            success=True,
            result=result_base64,
            processing_time=processing_time,
        )

    except Exception as e:
        logger.exception("Enhance error")
        return ProcessResponse(success=False, error=format_for_response(e))


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/api/enhance", response_model=ProcessResponse)
async def enhance_face(request: EnhanceRequest):
    """
    JSON endpoint — base64 image in `request.image`.
    """
    try:
        image_bytes = base64.b64decode(request.image)
    except Exception as e:
        logger.warning("Base64 decode failed: %s", e)
        return ProcessResponse(
            success=False,
            error=f"Invalid base64 image: {format_for_response(e)}",
        )

    return _process_image_bytes(
        image_bytes,
        request.upscale or 2,
        request.weight if request.weight is not None else 0.5,
    )


@router.post("/api/enhance/file", response_model=ProcessResponse)
async def enhance_face_file(
    file: UploadFile = File(...),
    upscale: int = Form(default=1, ge=1, le=4),
    weight: float = Form(default=0.5, ge=0.0, le=1.0),
):
    """
    Multipart endpoint — raw file upload.

    M3 — Form() ge/le constraints reject out-of-range values at the FastAPI
    boundary (HTTP 422). _process_image_bytes additionally rejects upscale=3
    (Form's ge/le can't express the {1,2,4} discrete set) with a 200 envelope.
    """
    contents = await file.read()
    return _process_image_bytes(contents, upscale, weight)
