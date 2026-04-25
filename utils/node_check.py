"""
Node.js availability check.

yt-dlp uses `--js-runtimes node` to run YouTube's JS signature extractors.
Without Node.js, most YouTube downloads fail with 403 / signature errors.

The probe is DEFERRED to first call (get_node_status()) so we don't pay a
subprocess cost on every Python import. The legacy module attributes
NODE_AVAILABLE and NODE_VERSION remain available via PEP 562 __getattr__ —
they trigger the probe lazily on first access.
"""
import logging
import subprocess
import sys
from typing import Optional

logger = logging.getLogger("mantra.agent.node")

# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress flashing CMD
# window when frozen GUI app spawns a console-subsystem child.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# (available, version_str_or_None) — None until first probe.
_node_check_cache: Optional[tuple[bool, Optional[str]]] = None
_warned_missing = False


def _do_check_node() -> tuple[bool, Optional[str]]:
    """Actual probe — runs `node --version` once per process."""
    try:
        r = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, (r.stdout or "").strip() or None
    except Exception:
        pass
    return False, None


def get_node_status() -> tuple[bool, Optional[str]]:
    """Return cached (available, version_str_or_None). Probes on first call."""
    global _node_check_cache, _warned_missing
    if _node_check_cache is None:
        _node_check_cache = _do_check_node()
        if not _node_check_cache[0] and not _warned_missing:
            _warned_missing = True
            logger.warning(
                "Node.js not found on PATH. YouTube endpoints may fail — "
                "install Node.js LTS from https://nodejs.org and restart the agent."
            )
    return _node_check_cache


# Backward-compatible module attributes (lazy via PEP 562).
# Existing `from utils.node_check import NODE_AVAILABLE, NODE_VERSION` continues
# to work but probe is deferred until the import statement actually evaluates.
def __getattr__(name: str):
    if name == "NODE_AVAILABLE":
        return get_node_status()[0]
    if name == "NODE_VERSION":
        return get_node_status()[1]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Legacy alias for the original eager-probe function name.
def check_node() -> tuple[bool, Optional[str]]:
    """Legacy alias — prefer get_node_status()."""
    return get_node_status()
