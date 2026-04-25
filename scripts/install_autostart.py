"""
Windows auto-start helper for Mantra Creative Agent tray.

Registers the tray launcher under HKCU Run so it starts at user login.
Per-user (HKCU), so no admin / UAC required.

Commands:
    py scripts/install_autostart.py install      # add to HKCU Run
    py scripts/install_autostart.py uninstall    # remove from HKCU Run
    py scripts/install_autostart.py status       # show current state
"""
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MantraAgent"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _agent_dir() -> Path:
    # scripts/ is one level below the agent root
    return Path(__file__).resolve().parent.parent


def _agent_command() -> str:
    """Build the autostart command string: '"<pythonw>" "<tray.py>"'."""
    agent_dir = _agent_dir()
    pyw = agent_dir / ".venv" / "Scripts" / "pythonw.exe"
    tray = agent_dir / "tray.py"
    return f'"{pyw}" "{tray}"'


def _check_paths() -> tuple[bool, str]:
    """Validate that pythonw + tray.py exist before installing."""
    agent_dir = _agent_dir()
    pyw = agent_dir / ".venv" / "Scripts" / "pythonw.exe"
    tray = agent_dir / "tray.py"
    if not pyw.exists():
        return False, f"pythonw.exe not found at {pyw} (run run_dev.bat first)"
    if not tray.exists():
        return False, f"tray.py not found at {tray}"
    return True, ""


def install() -> int:
    if not _is_windows():
        print("[error] Auto-start is Windows-only.")
        return 2
    ok, err = _check_paths()
    if not ok:
        print(f"[error] {err}")
        return 1

    import winreg

    cmd = _agent_command()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        print(f"[install] Registered HKCU\\{RUN_KEY}\\{VALUE_NAME}")
        print(f"          Value: {cmd}")
        print("[install] Will start at next login. Sign out / in to verify.")
        return 0
    except OSError as exc:
        print(f"[error] Failed to write registry: {exc}")
        return 1


def uninstall() -> int:
    if not _is_windows():
        print("[error] Auto-start is Windows-only.")
        return 2

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                print(f"[uninstall] Removed HKCU\\{RUN_KEY}\\{VALUE_NAME}")
                return 0
            except FileNotFoundError:
                print(f"[uninstall] Value {VALUE_NAME} not present — nothing to do.")
                return 0
    except OSError as exc:
        print(f"[error] Failed to access registry: {exc}")
        return 1


def status() -> int:
    if not _is_windows():
        print("[status] Not on Windows — auto-start is unsupported here.")
        return 0

    import winreg

    expected_cmd = _agent_command()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ,
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
                print(f"[status] Installed: HKCU\\{RUN_KEY}\\{VALUE_NAME}")
                print(f"         Value:    {value}")
                if value != expected_cmd:
                    print("[status] WARNING: stored command differs from current path.")
                    print(f"         Expected: {expected_cmd}")
                    print("         Run 'install' again to refresh.")
                return 0
            except FileNotFoundError:
                print("[status] Not installed.")
                print(f"         Would install: {expected_cmd}")
                return 0
    except OSError as exc:
        print(f"[error] Failed to read registry: {exc}")
        return 1


COMMANDS = {
    "install": install,
    "uninstall": uninstall,
    "status": status,
}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(__doc__)
        print(f"Available commands: {', '.join(COMMANDS)}")
        return 2
    return COMMANDS[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
