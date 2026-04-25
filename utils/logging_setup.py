"""
Mantra Creative Agent - centralized logging setup.

Writes to three rotating log files under config.LOG_DIR:
  * agent.log       - all INFO+ messages
  * access.log      - request access log (from request_log middleware)
  * error.log       - ERROR+ only (for quick crash triage)
  * crashes/<id>.log - individual stacktrace dumps (one per unhandled exception)

Plus stdout for interactive dev. Rotating: 10MB max, keep 5 backups.

Usage in main.py:
    from utils.logging_setup import setup_logging
    setup_logging()  # call once before importing handlers
"""
import logging
import logging.handlers
import sys
from pathlib import Path

import config

# Access logger - separate from root, never bubbles
ACCESS_LOGGER_NAME = "mantra.agent.access"


def setup_logging(level: str | None = None) -> None:
    """Configure root + access + error handlers. Idempotent."""
    level = (level or config.LOG_LEVEL).upper()
    level_int = getattr(logging, level, logging.INFO)

    # Ensure log dir exists (config already creates it, but be safe)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    crashes_dir = config.LOG_DIR / "crashes"
    crashes_dir.mkdir(exist_ok=True)

    # -- Root logger setup --------------------------------------------------
    root = logging.getLogger()
    # Remove any handlers that are already attached (idempotent - main.py may re-call)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level_int)

    fmt_main = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stdout handler (human-friendly)
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setLevel(level_int)
    stdout_h.setFormatter(fmt_main)
    root.addHandler(stdout_h)

    # Rotating agent.log - everything
    agent_file = logging.handlers.RotatingFileHandler(
        str(config.LOG_DIR / "agent.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    agent_file.setLevel(level_int)
    agent_file.setFormatter(fmt_main)
    root.addHandler(agent_file)

    # Rotating error.log - ERROR+ only
    error_file = logging.handlers.RotatingFileHandler(
        str(config.LOG_DIR / "error.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(fmt_main)
    root.addHandler(error_file)

    # -- Access logger - separate file, doesn't propagate to root -----------
    access = logging.getLogger(ACCESS_LOGGER_NAME)
    for h in list(access.handlers):
        access.removeHandler(h)
    access.setLevel(logging.INFO)
    access.propagate = False

    fmt_access = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    access_file = logging.handlers.RotatingFileHandler(
        str(config.LOG_DIR / "access.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    access_file.setFormatter(fmt_access)
    access.addHandler(access_file)

    # Also echo access to stdout at DEBUG level (dev aid)
    if level_int <= logging.DEBUG:
        access.addHandler(stdout_h)


def write_crash_report(request_id: str, exc_info, context: dict | None = None) -> Path:
    """Write a one-shot crash report to LOG_DIR/crashes/{request_id}.log. Returns path."""
    import traceback
    from datetime import datetime

    crashes_dir = config.LOG_DIR / "crashes"
    crashes_dir.mkdir(exist_ok=True)
    path = crashes_dir / f"{request_id}.log"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"Crash report - {datetime.now().isoformat()}\n")
        fh.write(f"request_id: {request_id}\n")
        if context:
            for k, v in context.items():
                fh.write(f"{k}: {v}\n")
        fh.write("\n--- traceback ---\n")
        traceback.print_exception(*exc_info, file=fh)
    return path
