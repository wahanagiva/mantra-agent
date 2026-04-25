"""
Phase 2 basicsr/gfpgan/facexlib installer — Py 3.13 compatible.

Why this script exists
----------------------
basicsr 1.4.2 (last release 2022, upstream abandoned) has a setup.py pattern
that is broken on Python 3.13:

    def get_version():
        with open(version_file, 'r') as f:
            exec(compile(f.read(), version_file, 'exec'))
        return locals()['__version__']       # <-- fails on Py 3.13 / PEP 667

Under PEP 667, `exec()` in a function scope no longer writes back to the
function's local namespace, so `locals()['__version__']` raises KeyError and
the wheel build aborts. gfpgan pins `basicsr>=1.4.2` so the dep chain dies.

This script downloads the basicsr sdist, patches those two lines to use an
explicit namespace dict, builds a wheel from the patched source, installs it,
then installs facexlib + gfpgan (which only need `import basicsr` to work at
runtime — the distribution is satisfied by our patched wheel).

Run AFTER `pip install -r requirements.txt` has installed the rest.
Usage:
    python scripts/install_phase2.py
    python scripts/install_phase2.py --dry-run   # show plan without installing
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


BASICSR_VERSION = "1.4.2"
FACEXLIB_VERSION = "0.3.0"
GFPGAN_VERSION = "1.3.8"

BASICSR_SDIST_URL = (
    f"https://files.pythonhosted.org/packages/source/b/basicsr/"
    f"basicsr-{BASICSR_VERSION}.tar.gz"
)


# The broken pattern vs. the PEP-667-safe replacement.
_OLD_GET_VERSION = (
    "def get_version():\n"
    "    with open(version_file, 'r') as f:\n"
    "        exec(compile(f.read(), version_file, 'exec'))\n"
    "    return locals()['__version__']"
)
_NEW_GET_VERSION = (
    "def get_version():\n"
    "    ns = {}\n"
    "    with open(version_file, 'r') as f:\n"
    "        exec(compile(f.read(), version_file, 'exec'), ns)\n"
    "    return ns['__version__']"
)


def _log(msg: str) -> None:
    print(f"[install_phase2] {msg}", flush=True)


def _run(cmd: list[str], *, dry_run: bool) -> None:
    _log("$ " + " ".join(cmd))
    if dry_run:
        return
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"command failed (exit {r.returncode}): {' '.join(cmd)}")


def _download(url: str, dest: Path) -> None:
    _log(f"download {url} -> {dest}")
    with urllib.request.urlopen(url, timeout=60) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def _patch_basicsr_setup(setup_py: Path) -> bool:
    """Return True if the file was modified, False if already patched."""
    text = setup_py.read_text(encoding="utf-8")
    if _OLD_GET_VERSION not in text:
        if "ns = {}" in text:
            _log("setup.py already patched — skipping")
            return False
        raise SystemExit(
            "basicsr setup.py does not match expected pattern — "
            "upstream may have changed; manual review required."
        )
    patched = text.replace(_OLD_GET_VERSION, _NEW_GET_VERSION)
    setup_py.write_text(patched, encoding="utf-8")
    _log("patched setup.py (get_version uses explicit namespace dict)")
    return True


def install_basicsr(pip_cmd: list[str], *, dry_run: bool) -> None:
    """Download basicsr sdist, patch setup.py, pip-install from local source."""
    # Skip if already installed at the correct version
    if not dry_run:
        r = subprocess.run(
            [sys.executable, "-c", "import basicsr; print(basicsr.__version__)"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip() == BASICSR_VERSION:
            _log(f"basicsr {BASICSR_VERSION} already installed — skipping")
            return

    with tempfile.TemporaryDirectory(prefix="basicsr_patch_") as td:
        tdp = Path(td)
        tarball = tdp / f"basicsr-{BASICSR_VERSION}.tar.gz"
        _download(BASICSR_SDIST_URL, tarball)

        _log("extract sdist")
        with tarfile.open(tarball, "r:gz") as tar:
            # `filter="data"` is the safe default from Py 3.12+ (PEP 706).
            # Older Pythons fall back to the legacy behavior automatically.
            try:
                tar.extractall(tdp, filter="data")
            except TypeError:
                tar.extractall(tdp)  # Py < 3.12

        src_dir = tdp / f"basicsr-{BASICSR_VERSION}"
        setup_py = src_dir / "setup.py"
        if not setup_py.is_file():
            raise SystemExit(f"setup.py not found in extracted sdist: {setup_py}")

        _patch_basicsr_setup(setup_py)

        _log("pip install patched basicsr")
        _run([*pip_cmd, "install", "--no-build-isolation", str(src_dir)], dry_run=dry_run)


def install_gfpgan_and_facexlib(pip_cmd: list[str], *, dry_run: bool) -> None:
    """Install facexlib then gfpgan from PyPI wheels."""
    _run([*pip_cmd, "install", f"facexlib=={FACEXLIB_VERSION}"], dry_run=dry_run)
    _run([*pip_cmd, "install", f"gfpgan=={GFPGAN_VERSION}"], dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the install plan without executing",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="python interpreter to use for pip (default: current)",
    )
    args = parser.parse_args()

    pip_cmd = [args.python, "-m", "pip"]

    _log(f"python: {args.python}")
    _log(f"Py version: {sys.version.split()[0]}")

    # Step 1: basicsr with patched setup.py
    install_basicsr(pip_cmd, dry_run=args.dry_run)
    # Step 2: facexlib + gfpgan (both have wheels, install cleanly)
    install_gfpgan_and_facexlib(pip_cmd, dry_run=args.dry_run)

    _log("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
