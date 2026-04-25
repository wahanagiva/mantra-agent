"""
Mantra Creative Agent - Configuration

All runtime settings, env-overridable. Single source of truth.
Mirror VPS contract while making local-friendly defaults.
"""
import os
import platform
import tempfile
from pathlib import Path


# ── Server ────────────────────────────────────────────────────────────────────
HOST = os.environ.get("MANTRA_AGENT_HOST", "127.0.0.1")  # localhost-only by default
PORT = int(os.environ.get("MANTRA_AGENT_PORT", "5555"))

# ── App metadata (mirror VPS values for compat) ───────────────────────────────
APP_TITLE = "Mantra Creative Agent"
APP_VERSION = "1.4.0"  # 1.3.0 + 14 bug fixes + thin installer architecture (small launcher + GitHub-hosted runtime zips)
APP_DESCRIPTION = "Local PC agent for Mantra Creative — runs heavy compute on user GPU/CPU"

# ── CORS ──────────────────────────────────────────────────────────────────────
# Production: only mantra.majutrah.co.id
# Dev: also allow localhost variants
_cors_extra = os.environ.get("MANTRA_AGENT_CORS_EXTRA", "")
CORS_ORIGINS = [
    "https://mantra.majutrah.co.id",
]
if _cors_extra:
    CORS_ORIGINS.extend([o.strip() for o in _cors_extra.split(",") if o.strip()])

# ── Phase 1 limits (mirror VPS) ───────────────────────────────────────────────
MAX_DURATION_SECONDS = 900  # 15 min — Phase 2 (yt-mp3)
MAX_BATCH_URLS = 5          # Phase 2
MAX_CONCURRENT_DOWNLOADS = 3  # Phase 2
MAX_MERGE_URLS = 30         # Phase 2

# ── Paths ─────────────────────────────────────────────────────────────────────
# Cross-platform temp dir (Windows: %LOCALAPPDATA%\Temp; Linux: /tmp)
TEMP_DIR = Path(tempfile.gettempdir())

# Persistent app data (cache, models, logs)
if platform.system() == "Windows":
    APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "MantraAgent"
else:
    APP_DATA_DIR = Path.home() / ".mantra-agent"

LOG_DIR = APP_DATA_DIR / "logs"
MODELS_DIR = APP_DATA_DIR / "models"
MERGE_SERVE_DIR = APP_DATA_DIR / "merge_serve"  # Phase 2

# Ensure dirs exist
for _d in (APP_DATA_DIR, LOG_DIR, MODELS_DIR, MERGE_SERVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── GPU / Compute ─────────────────────────────────────────────────────────────
# Force CPU mode? Set MANTRA_AGENT_FORCE_CPU=1 to disable GPU detection.
# Default: auto-detect — use CUDA if torch+CUDA available, else CPU.
FORCE_CPU = os.environ.get("MANTRA_AGENT_FORCE_CPU", "0") == "1"

# rembg ONNX provider preference (auto-fall-through)
# Order matters: tries first available
REMBG_PROVIDERS_GPU = ["CUDAExecutionProvider", "CPUExecutionProvider"]
REMBG_PROVIDERS_CPU = ["CPUExecutionProvider"]

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("MANTRA_AGENT_LOG_LEVEL", "INFO").upper()
LOG_FILE = LOG_DIR / "agent.log"

# ── External binaries (Phase 2) ───────────────────────────────────────────────
# Path to ffmpeg.exe — auto-discover via imageio-ffmpeg, or user override
FFMPEG_PATH = os.environ.get("MANTRA_AGENT_FFMPEG", "")  # empty = auto-discover
YTDLP_PATH = os.environ.get("MANTRA_AGENT_YTDLP", "yt-dlp")  # PATH-resolved by default
