"""
Auto-update logic for Mantra Creative Agent.

This module exposes building blocks for the update flow. The orchestration
itself lives in tray.py (the tray process owns the agent subprocess and is
the only place that can safely stop/restart it before file replacement).

Public API:
    check_for_update(timeout_s)        -> UpdateResult
    download_to(url, dest, expected_sha256, progress_cb) -> bool
    apply_patch(patch_zip, install_dir) -> bool
    apply_full_installer(installer_exe) -> bool

Tray flow (typical):
    1. result = check_for_update()
    2. if result.available and queue is idle (queried via /api/admin/system):
    3.     download_to(result.download_url, dl_path, result.download_sha256)
    4.     tray.stop_agent()                # release file locks
    5.     apply_patch(dl_path) or apply_full_installer(dl_path)
    6.     tray.start_agent()               # respawn from new files
    7.     show toast notification

Safety:
    * dev mode (sys.frozen False) — apply_* functions still work but are
      typically a no-op via skip in tray (refuse to overwrite source
      checkout). check_for_update + download_to remain useful for testing.
    * patch backup — install dir is copytree'd to {install_dir}_backup_{ts}
      before extraction; restored on any failure.
    * SHA256 verification on download AND on every patched file (sha256_after
      from manifest) — corrupted bytes never reach the live install.
    * stdlib only (urllib, hashlib, zipfile) — no third-party deps, so the
      updater itself never needs an update before it can update the agent.
"""
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Callable

import config

logger = logging.getLogger("mantra.agent.updater")

# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress flashing CMD
# window when frozen GUI app (no console) spawns the installer.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ── Install dir detection ─────────────────────────────────────────────────────
def _install_dir() -> Path:
    """
    Where the running agent is installed.

    * Bundled (PyInstaller/Nuitka, sys.frozen True): dir containing the
      executable — e.g. C:\\Program Files\\Mantra Creative\\Agent\\.
    * Dev (running from source): the agent/ folder (parent of utils/).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent  # agent/ folder


INSTALL_DIR = _install_dir()
IS_FROZEN = bool(getattr(sys, "frozen", False))
MANIFEST_URL = "https://mantra.majutrah.co.id/agent/version.json"
DOWNLOAD_TIMEOUT_S = 1800  # 30 min for big installer files
DOWNLOAD_CHUNK = 1024 * 1024  # 1 MiB


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class UpdateResult:
    available: bool = False
    current_version: str = ""
    latest_version: str = ""
    download_kind: str = ""        # 'patch' | 'installer' | ''
    download_url: Optional[str] = None
    download_sha256: Optional[str] = None
    file_size_bytes: Optional[int] = None
    mandatory: bool = False
    notes: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class UpdateError(Exception):
    """Raised internally during apply_patch when something verifiable fails."""
    pass


# ── Step 1: Check ─────────────────────────────────────────────────────────────
def check_for_update(timeout_s: float = 8.0) -> UpdateResult:
    """
    Fetch the server manifest and decide whether an update is available.

    Never raises — failures are reported via UpdateResult.error so callers
    (tray, healthchecks) can keep going.

    Prefers a delta patch when one is published for the current version;
    falls back to the full installer otherwise.
    """
    current = config.APP_VERSION
    result = UpdateResult(current_version=current)

    try:
        req = urllib.request.Request(
            MANIFEST_URL,
            headers={"User-Agent": f"MantraAgent/{current}"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        result.error = f"manifest fetch failed: {e}"
        return result

    latest = manifest.get("latest_version")
    if not latest:
        result.error = "manifest missing latest_version"
        return result
    result.latest_version = latest

    if not _semver_newer(latest, current):
        return result  # available stays False; nothing to do

    result.available = True
    result.mandatory = bool(manifest.get("mandatory", False))
    result.notes = manifest.get("notes")

    # Prefer patch if our current version has one published
    patches = manifest.get("patches") or {}
    patches_sha = manifest.get("patches_sha256") or {}
    if current in patches and patches[current]:
        result.download_kind = "patch"
        result.download_url = patches[current]
        result.download_sha256 = patches_sha.get(current)
    else:
        result.download_kind = "installer"
        result.download_url = manifest.get("download_url")
        result.download_sha256 = manifest.get("sha256")
        result.file_size_bytes = manifest.get("file_size_bytes")

    if not result.download_url:
        result.error = "manifest has no usable download_url"
        result.available = False

    return result


def _semver_newer(latest: str, current: str) -> bool:
    """True if `latest > current` by major.minor.patch comparison.

    Pre-release suffixes (1.3.0-beta) are stripped before compare. Any parse
    failure yields (0,0,0) so a malformed version is treated as oldest.
    """
    def tup(v: str) -> tuple:
        try:
            core = (v or "").split("-", 1)[0].split("+", 1)[0]
            parts = [int(x) for x in core.split(".") if x != ""]
            while len(parts) < 3:
                parts.append(0)
            return tuple(parts[:3])
        except Exception:
            return (0, 0, 0)
    return tup(latest) > tup(current)


# ── Step 2: Download with progress + SHA256 verify ────────────────────────────
def download_to(
    url: str,
    dest: Path,
    expected_sha256: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> bool:
    """
    Stream a URL to `dest` (atomic via .part swap), optionally verifying
    SHA256. Returns True on success, False on any failure (logged).

    progress_cb(sent_bytes, total_bytes) is invoked after each chunk;
    total_bytes is 0 if the server omits Content-Length.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "MantraAgent-Updater/1.0"}
        )
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r:
            try:
                total = int(r.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                total = 0
            sent = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    sent += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(sent, total)
                        except Exception:
                            # Never let a UI callback kill the download
                            logger.debug("progress_cb raised", exc_info=True)

        if expected_sha256:
            got = h.hexdigest()
            if got.lower() != expected_sha256.lower():
                logger.error(
                    "SHA256 mismatch for %s — got %s, expected %s",
                    url, got, expected_sha256,
                )
                _safe_unlink(tmp)
                return False

        tmp.replace(dest)
        return True
    except Exception:
        logger.exception("download failed: %s", url)
        _safe_unlink(tmp)
        return False


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("could not unlink %s", p, exc_info=True)


# ── Step 3: Apply patch (delta zip) ───────────────────────────────────────────
def apply_patch(patch_zip: Path, install_dir: Path = INSTALL_DIR) -> bool:
    """
    Extract a delta patch over `install_dir`. Steps:

      1. Validate zip + read manifest.json.
      2. Backup install_dir → {install_dir}_backup_{epoch_seconds}.
      3. Extract files_added + files_modified from files/ into install_dir.
      4. Delete files_removed.
      5. Verify sha256 of every file listed in sha256_after.
      6. On any failure: rmtree install_dir, copytree backup back in place.

    Returns True on success, False on failure (rollback attempted, logged).

    The caller (tray) is responsible for stopping the agent before calling
    so files in _internal/ are not held open by the live process.
    """
    patch_zip = Path(patch_zip)
    install_dir = Path(install_dir)
    logger.info("Applying patch %s -> %s", patch_zip, install_dir)

    if not patch_zip.exists():
        logger.error("patch zip missing: %s", patch_zip)
        return False
    if not install_dir.is_dir():
        logger.error("install dir missing: %s", install_dir)
        return False

    # 1. Read manifest
    try:
        with zipfile.ZipFile(patch_zip) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError:
        logger.error("patch zip missing manifest.json: %s", patch_zip)
        return False
    except Exception as e:
        logger.error("failed to read manifest from patch zip: %s", e)
        return False

    files_added = list(manifest.get("files_added") or [])
    files_modified = list(manifest.get("files_modified") or [])
    files_removed = list(manifest.get("files_removed") or [])
    sha_after = dict(manifest.get("sha256_after") or {})
    files_to_extract = files_added + files_modified

    # 2. Backup
    backup = install_dir.parent / f"{install_dir.name}_backup_{int(time.time())}"
    logger.info("Backing up %s -> %s", install_dir, backup)
    try:
        shutil.copytree(install_dir, backup, symlinks=False)
    except Exception as e:
        logger.error("backup failed: %s", e)
        return False

    try:
        # 3. Extract added + modified files
        with zipfile.ZipFile(patch_zip) as zf:
            for rel in files_to_extract:
                # Defend against zip-slip — rel is normalized + must stay
                # under install_dir
                target = (install_dir / rel).resolve()
                try:
                    target.relative_to(install_dir.resolve())
                except ValueError:
                    raise UpdateError(f"refusing to write outside install dir: {rel}")
                arc = "files/" + rel.replace("\\", "/")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(arc) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        # 4. Delete removed files
        for rel in files_removed:
            victim = (install_dir / rel).resolve()
            try:
                victim.relative_to(install_dir.resolve())
            except ValueError:
                logger.warning("ignoring out-of-tree files_removed entry: %s", rel)
                continue
            _safe_unlink(victim)

        # 5. Verify sha256 of every post-patch file the manifest lists
        for rel, expected in sha_after.items():
            f = install_dir / rel
            if not f.exists():
                raise UpdateError(f"missing after patch: {rel}")
            actual = hashlib.sha256(f.read_bytes()).hexdigest()
            if actual.lower() != str(expected).lower():
                raise UpdateError(
                    f"sha mismatch for {rel} — got {actual[:12]}..., "
                    f"expected {str(expected)[:12]}..."
                )

        logger.info(
            "Patch applied successfully (%d added, %d modified, %d removed). "
            "Backup retained at %s.",
            len(files_added), len(files_modified), len(files_removed), backup,
        )
        # Backup is intentionally kept; cleanup of *_backup_* dirs older than
        # ~1h happens in handlers/merge.py's periodic sweep.
        return True

    except Exception as e:
        logger.exception("Patch apply failed — rolling back: %s", e)
        try:
            shutil.rmtree(install_dir, ignore_errors=True)
            shutil.copytree(backup, install_dir, symlinks=False)
            logger.info("Rolled back from %s", backup)
        except Exception as e2:
            logger.exception(
                "ROLLBACK ALSO FAILED — install may be corrupt: %s", e2
            )
        return False


# ── Step 4: Apply full installer ──────────────────────────────────────────────
def apply_full_installer(installer_exe: Path) -> bool:
    """
    Run the Inno Setup installer silently. The installer (same AppId as the
    current install) replaces files in-place.

    Flags:
        /VERYSILENT          — no UI at all
        /NORESTART           — don't auto-reboot the machine
        /SUPPRESSMSGBOXES    — pair with /VERYSILENT to suppress any boxes
        /CLOSEAPPLICATIONS   — let Inno close the running agent.exe
        /RESTARTAPPLICATIONS — Inno will re-launch what it closed (tray will
                               also auto-respawn if it sees process death)
    """
    installer_exe = Path(installer_exe)
    if not installer_exe.exists():
        logger.error("installer exe missing: %s", installer_exe)
        return False
    try:
        proc = subprocess.run(
            [
                str(installer_exe),
                "/VERYSILENT",
                "/NORESTART",
                "/SUPPRESSMSGBOXES",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ],
            timeout=600,           # 10 min hard cap
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            logger.error(
                "installer exit %d: stderr=%s",
                proc.returncode, (proc.stderr or "")[:500],
            )
            return False
        logger.info("installer completed: %s", installer_exe)
        return True
    except subprocess.TimeoutExpired:
        logger.error("installer timed out after 600s: %s", installer_exe)
        return False
    except Exception:
        logger.exception("installer run failed: %s", installer_exe)
        return False
