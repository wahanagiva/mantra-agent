"""
FFmpeg discovery + availability check.

Resolution priority:
  1. MANTRA_AGENT_FFMPEG env var (config.FFMPEG_PATH)
  2. imageio-ffmpeg bundled binary (if imageio-ffmpeg installed)
  3. "ffmpeg" on system PATH

Used by handlers/merge.py and /api/health.

NOTE: FFMPEG_PATH resolution is EAGER (no subprocess — pure import-time work),
so module-level FFMPEG_PATH stays available immediately. The `-version` probe
that DOES spawn a subprocess is DEFERRED via get_ffmpeg_status().
"""
import logging
import shutil
import subprocess
import sys
from typing import Optional

import config

logger = logging.getLogger("mantra.agent.ffmpeg")

# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress flashing CMD.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def resolve_ffmpeg_path() -> str:
    """Return the ffmpeg executable path. Never raises — falls back to 'ffmpeg'."""
    if config.FFMPEG_PATH:
        return config.FFMPEG_PATH
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    except Exception as e:
        logger.warning("imageio_ffmpeg resolution failed: %s", e)
    # last resort: system PATH
    return "ffmpeg"


# (available, version_line_or_None) — None until first probe.
_ffmpeg_check_cache: Optional[tuple[bool, Optional[str]]] = None


def _do_check_ffmpeg() -> tuple[bool, Optional[str]]:
    """Actual subprocess probe — runs `<ffmpeg> -version` once per process."""
    path = resolve_ffmpeg_path()
    try:
        r = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0:
            first_line = (r.stdout or "").splitlines()[0] if r.stdout else ""
            return True, first_line.strip() or None
    except Exception:
        pass
    return False, None


def get_ffmpeg_status() -> tuple[bool, Optional[str]]:
    """Return cached (available, version_line_or_None). Probes on first call."""
    global _ffmpeg_check_cache
    if _ffmpeg_check_cache is None:
        _ffmpeg_check_cache = _do_check_ffmpeg()
    return _ffmpeg_check_cache


def check_ffmpeg() -> tuple[bool, Optional[str]]:
    """Legacy alias — prefer get_ffmpeg_status()."""
    return get_ffmpeg_status()


# Backward-compatible module attributes (lazy via PEP 562).
def __getattr__(name: str):
    if name == "FFMPEG_AVAILABLE":
        return get_ffmpeg_status()[0]
    if name == "FFMPEG_VERSION":
        return get_ffmpeg_status()[1]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Eagerly resolved (path-only, no subprocess) — safe at import time.
FFMPEG_PATH = resolve_ffmpeg_path()  # module-level constant (resolved once at import)
