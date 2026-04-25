"""
Pydantic request/response models — MIRROR EXACTLY from VPS gpu_api_server.py.

Any change here is a breaking API change for the PHP web app. Be careful.

Phase 1: only RemoveBgRequest + ProcessResponse are USED.
Phase 2 stubs included but commented to avoid drift.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ── Generic process response (used by remove-bg, enhance) ─────────────────────
class ProcessResponse(BaseModel):
    """
    Generic response for image-processing endpoints.
    Mirrors VPS gpu_api_server.py:141-145.

    `result` is base64-encoded PNG when success=True.
    `processing_time` is seconds (float).
    """
    success: bool
    result: Optional[str] = None
    error: Optional[str] = None
    processing_time: Optional[float] = None


# ── Remove BG ─────────────────────────────────────────────────────────────────
class RemoveBgRequest(BaseModel):
    """
    Mirrors VPS gpu_api_server.py:132-134.

    NOTE: VPS server IGNORES the `model` field and always uses u2net.
    Local agent honors the same behavior for compat — see handlers/remove_bg.py.
    """
    image: str  # base64 encoded
    model: Optional[str] = "u2net"


# ── Enhance Face (Phase 2) ────────────────────────────────────────────────────
class EnhanceRequest(BaseModel):
    """
    Mirrors VPS gpu_api_server.py:136-139.

    Validation (M3):
      * `upscale` is restricted to {1, 2, 4} — GFPGAN only supports those factors.
      * `weight` is restricted to [0.0, 1.0] — GFPGAN identity-preservation blend.
    Defaults preserved (upscale=1, weight=0.5) for backward compat.
    Note: handler falls back to upscale=2 when value is None/0; default here is 1
    to match Form() defaults below.
    """
    image: str  # base64 encoded
    upscale: Optional[Literal[1, 2, 4]] = 1
    weight: Optional[float] = Field(default=0.5, ge=0.0, le=1.0)


# ── YouTube models (Phase 2) ──────────────────────────────────────────────────
class YoutubeMp3Request(BaseModel):
    url: str


class YoutubeMp3Response(BaseModel):
    success: bool
    audio_base64: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[int] = None
    filesize: Optional[int] = None
    error: Optional[str] = None
    processing_time: Optional[float] = None


class YoutubeInfoResponse(BaseModel):
    success: bool
    title: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    view_count: Optional[int] = None
    description: Optional[str] = None
    error: Optional[str] = None


class YoutubeBatchRequest(BaseModel):
    urls: List[str] = Field(..., description="List of YouTube URLs (max 5)")


class YoutubeBatchItemResult(BaseModel):
    url: str
    success: bool
    audio_base64: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[int] = None
    filesize: Optional[int] = None
    error: Optional[str] = None


class YoutubeBatchResponse(BaseModel):
    success: bool
    results: List[YoutubeBatchItemResult]
    total: int
    successful: int
    failed: int
    processing_time: float


class YoutubeMergeRequest(BaseModel):
    urls: list = Field(..., description="YouTube URLs to merge (max 30)")
    titles: list = Field(None)
    album_name: str = Field("Full Album")
