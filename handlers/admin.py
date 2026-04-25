"""
Mantra Creative Agent — admin / introspection endpoints.

All endpoints under /api/admin/*.

Scope: operator debugging, tray-app UI, and future auto-updater. NOT exposed
via the public proxy (proxy.php only whitelists health/remove-bg/enhance/...).

Auth: OPEN for Phase 3 (agent listens on 127.0.0.1 only, loopback is safe).
Phase 4 may add a shared-secret header — TODO.
"""
import logging
import os
import platform
import sys

from fastapi import APIRouter

import config

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger("mantra.agent.admin")


@router.get("/info")
async def get_info():
    """Static build info. Useful for sanity checks."""
    return {
        "app_title": config.APP_TITLE,
        "app_version": config.APP_VERSION,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "host": config.HOST,
        "port": config.PORT,
        "cors_origins": config.CORS_ORIGINS,
        "force_cpu": config.FORCE_CPU,
        "app_data_dir": str(config.APP_DATA_DIR),
        "log_dir": str(config.LOG_DIR),
    }


@router.get("/system")
async def get_system():
    """Dynamic system + queue stats."""
    try:
        from utils.system_stats import get_all_stats
        return get_all_stats()
    except Exception as e:
        logger.exception("system stats failed")
        return {"error": str(e)}


@router.get("/version-check")
async def get_version_check():
    """Check for newer agent version on the remote manifest."""
    try:
        from utils.version_check import check_version
        result = check_version()
        return result.to_dict()
    except Exception as e:
        logger.exception("version-check failed")
        return {
            "current": config.APP_VERSION,
            "latest": None,
            "update_available": False,
            "mandatory": False,
            "download_url": None,
            "notes": None,
            "published_at": None,
            "error": str(e),
        }


@router.get("/ping")
async def ping():
    """Minimal liveness probe — no imports, no external calls."""
    return {"ok": True, "pid": os.getpid()}


@router.get("/_crash-test")
async def _crash_test():
    """
    Debug-only: trigger an unhandled exception to verify the crash handler
    writes a proper crash report file. Loopback-only because admin/* is.

    DO NOT expose this to the public proxy.
    """
    raise RuntimeError("Intentional crash from /api/admin/_crash-test — this is a test.")
