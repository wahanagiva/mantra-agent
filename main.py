#!/usr/bin/env python3
"""
Mantra Creative Agent — main entry point.

Local PC drop-in replacement for the VPS GPU FastAPI server.
Mirrors API contract exactly so the existing web app at mantra.majutrah.co.id
can talk to localhost:5555 with zero PHP changes (just smart-detect in JS).

Phase 1: /api/health + /api/remove-bg + /api/remove-bg/file
Phase 2: enhance, youtube-*, merge
Phase 3: tray, rotating logs, request log middleware, crash reports, admin endpoints
"""
import logging
import sys

# CRITICAL: apply compat shims BEFORE any handler/model import
from utils.monkey_patch import apply_all_patches

_patch_results = apply_all_patches()

# Suppress noisy ONNX/warnings (mirror VPS behavior)
import warnings
warnings.filterwarnings("ignore")
import os
os.environ.setdefault("PYTHONWARNINGS", "ignore")
os.environ.setdefault("ORT_DISABLE_LOGS", "1")

# CRITICAL: add torch's CUDA lib directory to DLL search path BEFORE importing
# onnxruntime. onnxruntime-gpu needs cublasLt64_12.dll / cudnn*.dll which PyTorch
# bundles in `torch/lib/` — but onnxruntime looks in PATH / default search dirs
# only. Without this, onnxruntime-gpu silently falls back to CPU provider with
# error: "Error loading onnxruntime_providers_cuda.dll which depends on
# cublasLt64_12.dll which is missing".
try:
    import torch
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(_torch_lib):
        try:
            os.add_dll_directory(_torch_lib)  # Windows 3.8+
        except AttributeError:
            # Non-Windows or older Python — fall through
            pass
        os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
except ImportError:
    pass

import io
_old_stderr = sys.stderr
sys.stderr = io.StringIO()
try:
    import onnxruntime
    onnxruntime.set_default_logger_severity(3)
except ImportError:
    pass
sys.stderr = _old_stderr

# Now safe to import the rest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import config

# ── Body-size limit (H4) ──────────────────────────────────────────────────────
# Configurable via MANTRA_AGENT_MAX_BODY_MB (default 50 MB). Reject oversized
# requests at the ASGI layer BEFORE FastAPI buffers them in memory — stops the
# 100 MB-into-PIL DoS vector.
_max_body_mb = int(os.environ.get("MANTRA_AGENT_MAX_BODY_MB", "50"))
MAX_BODY_BYTES = getattr(config, "MAX_BODY_BYTES", _max_body_mb * 1024 * 1024)


class BodySizeLimitMiddleware:
    """
    Pure-ASGI middleware that rejects requests whose body exceeds MAX_BODY_BYTES.

    Decision is made from headers BEFORE any body is consumed:
      * Content-Length > limit                → 413
      * Content-Length missing AND chunked TE → 413 (we don't accept streaming)
      * Otherwise                             → pass through
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        content_length = headers.get("content-length")
        transfer_encoding = headers.get("transfer-encoding", "").lower()

        too_big = False
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    too_big = True
            except ValueError:
                too_big = True
        elif "chunked" in transfer_encoding:
            # No Content-Length + chunked → reject (we don't expect streaming uploads)
            too_big = True

        if too_big:
            limit_mb = self.max_bytes // (1024 * 1024)
            body = (
                b'{"success": false, "error": "Request body too large '
                b'(max ' + str(limit_mb).encode() + b' MB)"}'
            )
            await send({
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)

# Phase 3: configure rotating logs FIRST so handler imports inherit the setup
from utils.logging_setup import setup_logging
setup_logging()

logger = logging.getLogger("mantra.agent")
logger.info("Compat patches applied: %s", _patch_results)
logger.info("App data dir: %s", config.APP_DATA_DIR)

from handlers import admin, enhance, health, merge, remove_bg, youtube
from middleware.crash_handler import register as register_crash_handler
from middleware.private_network import PrivateNetworkAccessMiddleware
from middleware.request_log import RequestLogMiddleware


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title=config.APP_TITLE,
    description=config.APP_DESCRIPTION,
    version=config.APP_VERSION,
)

# Middleware add order = innermost → outermost (Starlette LIFO).
# Request flow (outermost first):
#   PNA → RequestLog → CORS → routes
# CORS intercepts OPTIONS preflight and returns early; PNA then adds its header
# to that preflight response on the way out.

# CORS — innermost (handles OPTIONS preflight, returns canonical headers)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "X-Requested-With"],
    expose_headers=["X-Request-ID"],
)

# Phase 3: per-request access log + crash handler
app.add_middleware(RequestLogMiddleware)
register_crash_handler(app)

# H4: body-size limit — added between RequestLog and PNA. PNA must remain
# OUTERMOST (it augments preflight responses), so we add this BEFORE PNA so PNA
# wraps it. Body-size check still runs early (rejects oversized POSTs before
# they reach handlers / consume memory).
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)

# Chrome Private Network Access — OUTERMOST so it can augment the response CORS
# already produced for OPTIONS preflight (adds Access-Control-Allow-Private-Network).
app.add_middleware(PrivateNetworkAccessMiddleware)


# ── M1: validation error envelope ─────────────────────────────────────────────
# Wrap Pydantic validation failures in the standard {"success": false, "error": ...}
# envelope so the PHP frontend doesn't have to special-case FastAPI's default
# {"detail": [...]} format. Status 200 to match the rest of the API contract.
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first_err = errors[0] if errors else {}
    field = ".".join(str(p) for p in first_err.get("loc", []))
    msg = f"{first_err.get('msg', 'Invalid request')} (field: {field})"
    return JSONResponse(
        status_code=200,
        content={"success": False, "error": msg},
    )


# ── L5: override unhandled-exception handler ──────────────────────────────────
# Wraps middleware/crash_handler.unhandled_exception_handler to strip the
# redundant `request_id=...` substring from the error message. The crash report
# is still written by the original handler; we only rewrite the response body.
from middleware.crash_handler import unhandled_exception_handler as _orig_crash_handler


@app.exception_handler(Exception)
async def _crash_handler_no_redundant_id(request: Request, exc: Exception):
    response = await _orig_crash_handler(request, exc)
    # Original returns JSONResponse(content={"success": False, "error": "...
    # request_id=X", "request_id": X}). Rewrite the error string to drop the
    # request_id suffix; keep request_id only as the dedicated field.
    request_id = getattr(request.state, "request_id", "unknown")
    body = {
        "success": False,
        "error": "Internal server error (see agent logs)",
        "request_id": request_id,
    }
    return JSONResponse(status_code=response.status_code, content=body)

# ── Mount routers ─────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(remove_bg.router)
app.include_router(enhance.router)
app.include_router(youtube.router)
app.include_router(merge.router)
app.include_router(admin.router)

# Wire merge cleanup loop (startup hook)
merge.register_startup(app)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(
        "Starting %s v%s on http://%s:%d",
        config.APP_TITLE, config.APP_VERSION, config.HOST, config.PORT,
    )
    logger.info("CORS origins: %s", config.CORS_ORIGINS)
    # log_config=None → don't let uvicorn override our logging setup
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        log_config=None,
    )
