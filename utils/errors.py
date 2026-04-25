"""
User-facing error message mapping.

Raw exception / stderr → friendly message with action hint.

Goal: when the PHP web app shows a red toast, the text tells the user WHAT to do next,
not a Python traceback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FriendlyError:
    message: str       # short summary (1 sentence)
    action: str | None  # actionable hint (1 sentence) — None if nothing the user can do


def humanize_error(raw: str | BaseException) -> FriendlyError:
    """Map raw error text to a user-friendly message + action hint."""
    text = str(raw)
    lower = text.lower()

    # PIL: bytes that aren't a recognised image format. Match the exception
    # class first (so we don't get the BytesIO repr in the message) and then
    # the textual pattern as a belt-and-braces fallback.
    try:
        from PIL import UnidentifiedImageError  # local import to avoid hard dep
        if isinstance(raw, UnidentifiedImageError):
            return FriendlyError(
                "Format gambar tidak dikenali.",
                "Gunakan PNG, JPEG, WebP, BMP atau GIF."
            )
    except Exception:
        pass
    if "cannot identify image file" in lower:
        return FriendlyError(
            "Format gambar tidak dikenali.",
            "Gunakan PNG, JPEG, WebP, BMP atau GIF."
        )

    # CUDA OOM
    if "cuda out of memory" in lower or "out of memory" in lower:
        return FriendlyError(
            "GPU out of memory",
            "Try a smaller image, close other GPU-using apps, or switch to CPU mode (set MANTRA_AGENT_FORCE_CPU=1)."
        )

    # yt-dlp specific. Match SPECIFIC, FINAL errors first; only fall through
    # to the generic 429/rate-limit branch if nothing more specific matched.
    # yt-dlp emits transient `WARNING ... 429` lines BEFORE the real fatal
    # `ERROR ... is unavailable` — so for the rate-limit branch we restrict
    # the match to lines that begin with "ERROR:" (yt-dlp's fatal level).
    # Tested patterns are case-insensitive against `lower`.
    if ("video unavailable" in lower
            or "this video is not available" in lower
            or "is unavailable" in lower
            or "video is unavailable" in lower):
        return FriendlyError(
            "Video tidak tersedia (mungkin di-remove atau private).",
            "Double-check the URL is public and not region-locked."
        )
    if "sign in to confirm your age" in lower or "age-restricted" in lower:
        return FriendlyError(
            "Video age-restricted, tidak bisa di-download tanpa login.",
            "Use a non-age-restricted video."
        )
    if "private video" in lower:
        return FriendlyError("Video private.", "Use a public URL.")
    if "members-only" in lower or "members-only content" in lower:
        return FriendlyError("Video members-only.", "Use a public URL.")

    # Generic rate-limit / 403 — only consider FATAL "ERROR:" lines, not
    # transient "WARNING:" lines that yt-dlp prints during retries.
    error_lines = [ln for ln in text.splitlines() if ln.lstrip().lower().startswith("error:")]
    error_blob = "\n".join(error_lines).lower()
    if (("http error 429" in error_blob)
            or ("too many requests" in error_blob)
            or ("http error 403" in error_blob)
            or ("rate" in error_blob and "limit" in error_blob)):
        return FriendlyError(
            "YouTube rate-limited or blocked this request.",
            "Wait a few minutes and retry, or try a different video."
        )

    # Network
    if "timed out" in lower or "timeout" in lower:
        return FriendlyError("The operation timed out.", "Check your internet connection and try again.")
    if "connection refused" in lower:
        return FriendlyError("Connection refused.", "Check that the service is running.")
    if "nodename nor servname" in lower or "name or service not known" in lower:
        return FriendlyError("DNS lookup failed.", "Check your internet connection.")

    # ffmpeg
    if "ffmpeg" in lower and ("no such file" in lower or "not found" in lower):
        return FriendlyError(
            "ffmpeg binary not found.",
            "Install ffmpeg or make sure imageio-ffmpeg is installed in the venv."
        )

    # Node.js for yt-dlp
    if "node" in lower and ("not found" in lower or "is not recognized" in lower):
        return FriendlyError(
            "Node.js not installed.",
            "Install Node.js LTS from https://nodejs.org for YouTube endpoints."
        )

    # Permission / file-lock (Windows)
    if "permission denied" in lower or "being used by another process" in lower:
        return FriendlyError(
            "File locked or permission denied.",
            "Close any other program using that file, or run agent as administrator."
        )

    # Disk
    if "no space left on device" in lower:
        return FriendlyError("Disk full.", "Free up disk space (at least 1GB for models + temp).")

    # GFPGAN model
    if "gfpganv1.4.pth" in lower and ("download" in lower or "404" in lower):
        return FriendlyError(
            "GFPGAN model failed to download.",
            "Check internet connection and retry; the model downloads on first /api/enhance call."
        )

    # Fallback
    return FriendlyError(text[:200].strip() or "Unknown error", None)


def format_for_response(raw: str | BaseException) -> str:
    """Produce a single string suitable for ProcessResponse.error or similar."""
    fe = humanize_error(raw)
    if fe.action:
        return f"{fe.message} — {fe.action}"
    return fe.message
