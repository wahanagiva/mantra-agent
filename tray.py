"""
Mantra Creative Agent — Windows system-tray launcher.

Spawns the FastAPI agent (uvicorn via main.py) as a subprocess, then
shows a status dot in the system tray. The user controls the agent
lifecycle (restart / quit / open dashboard) via the tray menu.

Usage:
    pythonw tray.py        # via run_tray.bat (no console)
    python tray.py         # for debugging (console attached)

Requires: pystray>=0.19.5, Pillow, httpx
"""
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
import httpx

import config


# ── Logging ───────────────────────────────────────────────────────────────────
# Tray-specific log (separate from agent.log). Append mode, UTF-8.
_TRAY_LOG_PATH = config.APP_DATA_DIR / "tray.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] tray: %(message)s",
    handlers=[
        logging.FileHandler(_TRAY_LOG_PATH, mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mantra.tray")


# ── Platform helpers ──────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform.startswith("win")
# Windows: subprocess.CREATE_NO_WINDOW = 0x08000000 — suppress console window
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def _open_path(path: str):
    """Cross-platform 'open in file manager / browser'."""
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        logger.exception("Failed to open path: %s", path)


# ── Icons (pre-rendered) ──────────────────────────────────────────────────────
def _make_icon(color_hex: str) -> Image.Image:
    """Generate a 64x64 solid-color circle with a dark backing disc."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # outline / backing disc
    d.ellipse((4, 4, 60, 60), fill="#1f2937", outline=None)
    # fill
    r, g, b = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    d.ellipse((8, 8, 56, 56), fill=(r, g, b))
    return img


ICON_HEALTHY = _make_icon("#10b981")   # green
ICON_STOPPED = _make_icon("#ef4444")   # red
ICON_STARTING = _make_icon("#f59e0b")  # yellow


# ── Main tray app ─────────────────────────────────────────────────────────────
class TrayApp:
    POLL_INTERVAL_S = 5.0
    HEALTH_TIMEOUT_S = 2.0
    SHUTDOWN_GRACE_S = 5

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.health_data: dict | None = None
        self.is_healthy: bool = False
        self.poll_thread: threading.Thread | None = None
        self.stop_poll = threading.Event()
        self.agent_log_path = config.APP_DATA_DIR / "tray_agent.log"
        self._agent_log_handle = None
        # ── Auto-update state ─────────────────────────────────────────────────
        self.update_check_interval_s = 6 * 60 * 60  # 6 hours
        self.last_update_check = 0.0
        # idle | checking | downloading | applying | available | up_to_date | error
        self.update_state: str = "idle"
        self.latest_version: str | None = None
        self.update_thread: threading.Thread | None = None
        # Serialize check/apply across update_thread + menu-click thread
        self._update_lock = threading.Lock()
        self.icon = pystray.Icon(
            "mantra-agent",
            icon=ICON_STARTING,
            title=self._tooltip(),
            menu=self._build_menu(),
        )

    # ── Subprocess management ─────────────────────────────────────────────────
    def _resolve_agent_command(self) -> tuple[list[str], Path] | None:
        """
        Resolve how to launch the agent. Returns ([argv], cwd) or None if unresolvable.

        Two modes:
          * FROZEN (PyInstaller bundle): sibling agent.exe in the same folder
            as the running tray binary (sys.frozen is True in packaged form).
          * DEV (source checkout): .venv/Scripts/python.exe + main.py.
        """
        if getattr(sys, "frozen", False):
            # Bundled — locate agent.exe next to MantraAgentTray.exe
            # sys.executable is the frozen binary itself.
            bundle_dir = Path(sys.executable).resolve().parent
            agent_exe = bundle_dir / ("agent.exe" if IS_WINDOWS else "agent")
            if not agent_exe.exists():
                logger.error("Bundled agent binary not found at %s", agent_exe)
                return None
            return [str(agent_exe)], bundle_dir

        # Dev mode - use the venv python + main.py.
        # Resolution order:
        #   1. sys.executable - if tray.py is itself running under the venv python
        #      (this is how the bat installer spawns it).
        #   2. agent_dir/.venv/... - original dev-checkout layout.
        #   3. agent_dir/../venv/... - bat installer layout (src/ + venv/ siblings
        #      under %LOCALAPPDATA%\MantraAgent\).
        agent_dir = Path(__file__).resolve().parent
        main_py = agent_dir / "main.py"
        if not main_py.exists():
            logger.error("main.py not found at %s", main_py)
            return None

        candidates = [Path(sys.executable)]
        if IS_WINDOWS:
            candidates += [
                agent_dir / ".venv" / "Scripts" / "python.exe",
                agent_dir.parent / "venv" / "Scripts" / "python.exe",  # bat installer layout
            ]
        else:
            candidates += [
                agent_dir / ".venv" / "bin" / "python",
                agent_dir.parent / "venv" / "bin" / "python",
            ]
        python_exe = next((p for p in candidates if p.exists()), None)
        if python_exe is None:
            logger.error("Python venv not found. Tried: %s", [str(c) for c in candidates])
            return None
        return [str(python_exe), str(main_py)], agent_dir

    def start_agent(self) -> bool:
        """Spawn the agent subprocess (frozen: agent.exe; dev: venv python main.py).

        Returns True on launch (not necessarily 'healthy' — that's polled).
        """
        if self.proc is not None and self.proc.poll() is None:
            logger.info("Agent already running (pid=%s); skipping start", self.proc.pid)
            return True

        resolved = self._resolve_agent_command()
        if resolved is None:
            return False
        argv, cwd = resolved

        # Open log handle (append; line-buffered so tail -f shows progress)
        try:
            self._agent_log_handle = open(
                self.agent_log_path, "a", encoding="utf-8", buffering=1
            )
            self._agent_log_handle.write(
                f"\n--- agent launch @ {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            )
            self._agent_log_handle.flush()
        except Exception:
            logger.exception("Failed to open agent log file %s", self.agent_log_path)
            self._agent_log_handle = None

        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=self._agent_log_handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                close_fds=False,
            )
            logger.info("Started agent subprocess pid=%s (argv0=%s)", self.proc.pid, argv[0])
            return True
        except Exception:
            logger.exception("Failed to start agent subprocess")
            self.proc = None
            return False

    def stop_agent(self, wait_s: int | None = None) -> None:
        """Stop the agent subprocess gracefully, then SIGKILL if needed."""
        wait_s = wait_s if wait_s is not None else self.SHUTDOWN_GRACE_S
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is not None:
            logger.info("Agent already exited (rc=%s)", proc.returncode)
            self.proc = None
            self._close_agent_log()
            return

        logger.info("Stopping agent subprocess pid=%s (grace %ss)", proc.pid, wait_s)
        try:
            # subprocess.terminate() == TerminateProcess on Windows / SIGTERM on POSIX
            proc.terminate()
        except Exception:
            logger.exception("terminate() failed for pid=%s", proc.pid)

        try:
            proc.wait(timeout=wait_s)
            logger.info("Agent exited cleanly (rc=%s)", proc.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("Agent did not exit within %ss — killing", wait_s)
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                logger.exception("kill() failed for pid=%s", proc.pid)
        finally:
            self.proc = None
            self._close_agent_log()

    def _close_agent_log(self) -> None:
        if self._agent_log_handle is not None:
            try:
                self._agent_log_handle.flush()
                self._agent_log_handle.close()
            except Exception:
                pass
            self._agent_log_handle = None

    def restart_agent(self) -> None:
        logger.info("Restart requested")
        self.stop_agent()
        # Briefly mark as starting in the UI
        self.is_healthy = False
        self.health_data = None
        self._refresh_icon(ICON_STARTING)
        self.start_agent()

    # ── Health polling ────────────────────────────────────────────────────────
    def poll_loop(self) -> None:
        """Background thread: GET /api/health every POLL_INTERVAL_S."""
        url = f"http://127.0.0.1:{config.PORT}/api/health"
        # Small initial delay so uvicorn has a chance to bind
        self.stop_poll.wait(1.0)

        while not self.stop_poll.is_set():
            healthy = False
            data: dict | None = None
            try:
                with httpx.Client(timeout=self.HEALTH_TIMEOUT_S) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        healthy = data.get("status") == "healthy"
            except Exception as exc:
                logger.debug("Health check failed: %s", exc)

            # Detect unexpected subprocess exit
            if self.proc is not None and self.proc.poll() is not None:
                logger.warning(
                    "Agent subprocess exited unexpectedly (rc=%s) — staying in tray",
                    self.proc.returncode,
                )
                # Drop the dead handle but keep the tray alive for restart
                self.proc = None
                self._close_agent_log()
                healthy = False
                data = None

            self.is_healthy = healthy
            self.health_data = data
            self._refresh_icon(ICON_HEALTHY if healthy else ICON_STOPPED)

            # Wait POLL_INTERVAL_S, but wake immediately on shutdown
            self.stop_poll.wait(self.POLL_INTERVAL_S)

    def _refresh_icon(self, image: Image.Image) -> None:
        """Update the tray icon image, tooltip, and menu safely.

        If an update is available/in-progress, prefer the yellow "STARTING"
        icon to badge the tray — otherwise honor the caller's image (which
        reflects health state from the poll loop).
        """
        try:
            # Update state takes priority for badging while pending/in-flight
            if self.update_state in ("available", "downloading", "applying"):
                effective = ICON_STARTING
            else:
                effective = image
            self.icon.icon = effective
            self.icon.title = self._tooltip()
            # Rebuild menu so disabled status labels reflect the latest state
            self.icon.menu = self._build_menu()
            # update_menu pokes pystray to redraw on platforms that need it
            try:
                self.icon.update_menu()
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to refresh icon")

    def _refresh_menu(self) -> None:
        """Refresh icon + menu from current state (used by update flow).

        Picks the icon image from the latest health snapshot, then defers
        to _refresh_icon for the update-state badging logic.
        """
        if not self.icon:
            return
        # Pick base icon from current health state
        if self.health_data and self.health_data.get("status") == "healthy":
            base = ICON_HEALTHY
        elif self.is_healthy:
            base = ICON_HEALTHY
        else:
            base = ICON_STOPPED
        self._refresh_icon(base)

    # ── Tooltip / menu builders ───────────────────────────────────────────────
    def _tooltip(self) -> str:
        if self.is_healthy and self.health_data:
            mode = self.health_data.get("mode", "?")
            ver = self.health_data.get("agent_version", config.APP_VERSION)
            return f"Mantra Agent v{ver} — Running ({mode})"
        return "Mantra Agent — Not Running"

    def _status_label(self) -> str:
        return "Status: Running" if self.is_healthy else "Status: Stopped"

    def _version_label(self) -> str:
        if self.health_data:
            ver = self.health_data.get("agent_version", config.APP_VERSION)
            mode = self.health_data.get("mode", "?")
        else:
            ver = config.APP_VERSION
            mode = "—"
        return f"Version: {ver}  |  Mode: {mode}"

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Mantra Creative Agent", None, enabled=False),
            pystray.MenuItem(self._status_label(), None, enabled=False),
            pystray.MenuItem(self._version_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                self._update_menu_label(),
                self._on_update_click,
                enabled=self._update_actionable(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Dashboard", self._on_open_dashboard),
            pystray.MenuItem("Open Agent Health", self._on_open_health),
            pystray.MenuItem("Open Logs Folder", self._on_open_logs),
            pystray.MenuItem("Open App Data", self._on_open_appdata),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Agent", self._on_restart),
            pystray.MenuItem("Quit", self._on_quit),
        )

    # ── Update menu helpers ───────────────────────────────────────────────────
    def _update_menu_label(self) -> str:
        s = self.update_state
        if s == "up_to_date":
            return "\u2713 Up to date"
        if s == "checking":
            return "Checking for updates\u2026"
        if s == "downloading":
            return "Downloading update\u2026"
        if s == "applying":
            return "Installing update\u2026"
        if s == "available":
            ver = self.latest_version or "?"
            return f"Update available: v{ver} (click to install)"
        if s == "error":
            return "Update failed \u2014 Click to retry"
        return "Check for Updates Now"  # idle

    def _update_actionable(self) -> bool:
        """Whether the update menu item should be clickable."""
        return self.update_state in ("idle", "up_to_date", "available", "error")

    # ── Menu callbacks ────────────────────────────────────────────────────────
    def _on_open_dashboard(self, icon, item):
        webbrowser.open("https://mantra.majutrah.co.id")

    def _on_open_health(self, icon, item):
        webbrowser.open(f"http://127.0.0.1:{config.PORT}/api/health")

    def _on_open_logs(self, icon, item):
        _open_path(str(config.LOG_DIR))

    def _on_open_appdata(self, icon, item):
        _open_path(str(config.APP_DATA_DIR))

    def _on_restart(self, icon, item):
        # Run in a background thread so the tray UI stays responsive
        threading.Thread(target=self.restart_agent, daemon=True).start()

    def _on_quit(self, icon, item):
        logger.info("Quit requested from tray menu")
        # Stop polling first so it doesn't fight the shutdown
        self.stop_poll.set()
        # Stop the subprocess synchronously (may take up to SHUTDOWN_GRACE_S)
        try:
            self.stop_agent()
        finally:
            icon.stop()

    def _on_update_click(self, icon, item):
        """User clicked the update menu item — force a check + apply."""
        threading.Thread(
            target=lambda: self._check_and_apply_update(force=True),
            name="update-on-demand",
            daemon=True,
        ).start()

    # ── Toast notifications ───────────────────────────────────────────────────
    def _show_toast(self, title: str, message: str) -> None:
        """Show Windows toast notification via pystray's notify() API.

        pystray.Icon.notify() uses the native Windows shell notification API
        on Windows 10+. On unsupported platforms it silently no-ops.
        """
        try:
            if self.icon:
                self.icon.notify(message, title=title)
        except Exception:
            logger.exception("toast failed")

    # ── Idle check ────────────────────────────────────────────────────────────
    def _is_agent_busy(self) -> bool:
        """Query /api/admin/system for active jobs.

        True if there are merge jobs currently processing. If the agent is
        unreachable, returns False (better to attempt the update than block
        forever on a dead agent).
        """
        try:
            r = httpx.get(
                f"http://127.0.0.1:{config.PORT}/api/admin/system",
                timeout=2.0,
            )
            if r.status_code != 200:
                return False
            q = r.json().get("queue", {}) or {}
            return int(q.get("merge_jobs_processing", 0) or 0) > 0
        except Exception:
            return False

    # ── Auto-update polling ───────────────────────────────────────────────────
    def _update_check_loop(self) -> None:
        """Background: check for updates on startup + every 6 hours."""
        # Initial delay so the agent finishes booting + sidebar polling settles
        for _ in range(60):
            if self.stop_poll.is_set():
                return
            time.sleep(1)

        while not self.stop_poll.is_set():
            try:
                self._check_and_apply_update()
            except Exception:
                logger.exception("Update check loop crashed")
            # Wait next interval; bail early if shutdown signaled
            for _ in range(self.update_check_interval_s):
                if self.stop_poll.is_set():
                    return
                time.sleep(1)

    def _check_and_apply_update(self, force: bool = False) -> None:
        """Single-shot: check, decide, download, apply, restart.

        Thread-safe: serialized via self._update_lock so concurrent calls
        from the timer thread and a menu click can't collide.
        """
        # Don't block; if another check is already running, skip silently.
        if not self._update_lock.acquire(blocking=False):
            logger.debug("Update check already in progress; skipping")
            return
        try:
            self._do_check_and_apply_update(force=force)
        finally:
            self._update_lock.release()

    def _do_check_and_apply_update(self, force: bool = False) -> None:
        """Inner update flow — assumes _update_lock is already held."""
        try:
            from utils.updater import (
                check_for_update,
                download_to,
                apply_patch,
                apply_full_installer,
                IS_FROZEN,
                INSTALL_DIR,
            )
        except Exception:
            logger.exception("Updater module unavailable; skipping update check")
            return

        if not IS_FROZEN:
            logger.debug("Skipping update check \u2014 dev mode")
            return

        self.update_state = "checking"
        self._refresh_menu()

        result = check_for_update()
        self.last_update_check = time.time()

        if getattr(result, "error", None):
            logger.warning("Update check error: %s", result.error)
            self.update_state = "error"
            self._refresh_menu()
            return

        if not result.available:
            logger.info(
                "Up to date (v%s)", getattr(result, "current_version", "?")
            )
            self.update_state = "up_to_date"
            self._refresh_menu()
            return

        self.latest_version = result.latest_version
        self.update_state = "available"
        self._refresh_menu()
        self._show_toast(
            f"Mantra Agent v{result.latest_version} available",
            "Downloading\u2026",
        )

        # Smart guard: skip if agent busy (deferred until next interval)
        if not force and self._is_agent_busy():
            logger.info(
                "Update available but agent busy \u2014 will retry on next interval"
            )
            return

        # Download
        self.update_state = "downloading"
        self._refresh_menu()
        fname = result.download_url.rsplit("/", 1)[-1]
        tmp_dir = Path(tempfile.gettempdir()) / "mantra_agent_update"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dl_path = tmp_dir / fname

        ok = download_to(
            result.download_url,
            dl_path,
            expected_sha256=result.download_sha256,
        )
        if not ok:
            logger.error("Update download/verify failed")
            self.update_state = "error"
            self._refresh_menu()
            self._show_toast(
                "Update failed",
                f"Could not download v{result.latest_version}",
            )
            return

        # Stop agent (file lock concern for patch apply)
        self.update_state = "applying"
        self._refresh_menu()
        logger.info("Stopping agent for update apply")
        self.stop_agent(wait_s=10)

        # Apply
        if result.download_kind == "patch":
            applied = apply_patch(dl_path, INSTALL_DIR)
        else:
            applied = apply_full_installer(dl_path)

        if not applied:
            # Restart old agent so user isn't stranded
            self.start_agent()
            self.update_state = "error"
            self._refresh_menu()
            self._show_toast(
                "Update failed",
                f"v{result.latest_version} did not apply \u2014 rolled back",
            )
            return

        # Clean up downloaded file (best-effort)
        try:
            dl_path.unlink()
        except Exception:
            pass

        # Restart with new version
        logger.info("Update applied. Restarting agent.")
        self.start_agent()

        self.update_state = "up_to_date"
        self._refresh_menu()
        self._show_toast(
            f"Mantra Agent updated to v{result.latest_version}",
            "Now running the latest version",
        )

    # ── Run loop ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info(
            "Starting Mantra Agent tray (port=%s, app_data=%s)",
            config.PORT, config.APP_DATA_DIR,
        )
        started = self.start_agent()
        if not started:
            logger.error("Agent failed to start; tray will still run for diagnostics")

        self.stop_poll.clear()
        self.poll_thread = threading.Thread(
            target=self.poll_loop, name="health-poller", daemon=True,
        )
        self.poll_thread.start()

        # Auto-update checker (background; no-ops in dev mode)
        self.update_thread = threading.Thread(
            target=self._update_check_loop, name="update-checker", daemon=True,
        )
        self.update_thread.start()

        try:
            # Blocks until icon.stop() is called
            self.icon.run()
        finally:
            self.stop_poll.set()
            self.stop_agent()
            if self.poll_thread is not None:
                self.poll_thread.join(timeout=3)
            if self.update_thread is not None:
                self.update_thread.join(timeout=3)
            logger.info("Tray exited cleanly")


def main() -> int:
    try:
        TrayApp().run()
        return 0
    except Exception:
        logger.exception("Tray crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
