"""
Agent version-check helper.

Fetches the remote manifest (published at MANIFEST_URL) and compares to the
currently running version. Returns a structured result used by the admin
endpoint and (eventually) the auto-updater.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger("mantra.agent.version")

MANIFEST_URL = "https://mantra.majutrah.co.id/agent/version.json"
MANIFEST_TIMEOUT_S = 5.0


@dataclass
class VersionCheckResult:
    current: str
    latest: Optional[str]
    update_available: bool
    mandatory: bool
    download_url: Optional[str]
    notes: Optional[str]
    published_at: Optional[str]
    error: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _semver_tuple(v: str) -> tuple:
    """
    Parse '1.2.3' / '1.2.3-beta.1' into a comparable tuple.
    Invalid versions yield (0, 0, 0) — always "older".
    """
    try:
        core = v.split("-", 1)[0]
        parts = [int(x) for x in core.split(".")]
        # pad to 3
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (0, 0, 0)


def _is_newer(latest: str, current: str) -> bool:
    return _semver_tuple(latest) > _semver_tuple(current)


def check_version(timeout: float = MANIFEST_TIMEOUT_S) -> VersionCheckResult:
    """Fetch the manifest; compare; return structured result (never raises)."""
    current = config.APP_VERSION

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(MANIFEST_URL)
        if r.status_code != 200:
            return VersionCheckResult(
                current=current, latest=None, update_available=False,
                mandatory=False, download_url=None, notes=None,
                published_at=None,
                error=f"manifest HTTP {r.status_code}",
            )
        data = r.json()
    except Exception as e:
        logger.debug("version manifest fetch failed: %s", e)
        return VersionCheckResult(
            current=current, latest=None, update_available=False,
            mandatory=False, download_url=None, notes=None,
            published_at=None, error=str(e),
        )

    latest = data.get("latest_version")
    if not isinstance(latest, str):
        return VersionCheckResult(
            current=current, latest=None, update_available=False,
            mandatory=False, download_url=None, notes=None,
            published_at=None, error="manifest missing latest_version",
        )

    return VersionCheckResult(
        current=current,
        latest=latest,
        update_available=_is_newer(latest, current),
        mandatory=bool(data.get("mandatory", False)),
        download_url=data.get("download_url"),
        notes=data.get("notes"),
        published_at=data.get("published_at"),
        error=None,
    )
