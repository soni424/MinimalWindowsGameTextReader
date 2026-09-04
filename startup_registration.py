"""Current-user Windows startup registration for the portable reader."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import winreg


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "GameTextReader"
STARTUP_ARGUMENT = "--startup"


class StartupRegistrationError(RuntimeError):
    """Raised when Windows refuses to read or change startup registration."""


def build_startup_command(
    executable: str | Path | None = None,
    script: str | Path | None = None,
    *,
    frozen: bool | None = None,
    gui_executable: str | Path | None = None,
) -> str:
    """Return a correctly quoted Run-key command for packaged or source mode."""

    executable_path = Path(executable or sys.executable).resolve()
    packaged = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if packaged:
        arguments = [str(executable_path), STARTUP_ARGUMENT]
    else:
        script_path = Path(script or Path(__file__).with_name("main.py")).resolve()
        if gui_executable is not None:
            launcher = Path(gui_executable).resolve()
        else:
            pythonw = executable_path.with_name("pythonw.exe")
            launcher = pythonw if pythonw.exists() else executable_path
        arguments = [str(launcher), str(script_path), STARTUP_ARGUMENT]
    return subprocess.list2cmdline(arguments)


class StartupRegistration:
    """Own the one Run-key value used to launch this copy at Windows sign-in."""

    def __init__(self, command: str | None = None, registry: Any = winreg) -> None:
        self.command = command or build_startup_command()
        self._registry = registry

    def is_enabled(self) -> bool:
        """Return whether the registered value exactly matches this app command."""

        try:
            with self._registry.OpenKey(
                self._registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self._registry.KEY_READ,
            ) as key:
                value, value_type = self._registry.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StartupRegistrationError(
                f"Windows could not read the startup setting: {exc}"
            ) from exc
        return value_type in {
            self._registry.REG_SZ,
            getattr(self._registry, "REG_EXPAND_SZ", self._registry.REG_SZ),
        } and str(value) == self.command

    def set_enabled(self, enabled: bool) -> None:
        """Create or remove only this app's current-user startup value."""

        try:
            if enabled:
                with self._registry.CreateKeyEx(
                    self._registry.HKEY_CURRENT_USER,
                    RUN_KEY,
                    0,
                    self._registry.KEY_SET_VALUE,
                ) as key:
                    self._registry.SetValueEx(
                        key,
                        VALUE_NAME,
                        0,
                        self._registry.REG_SZ,
                        self.command,
                    )
                return

            try:
                with self._registry.OpenKey(
                    self._registry.HKEY_CURRENT_USER,
                    RUN_KEY,
                    0,
                    self._registry.KEY_SET_VALUE,
                ) as key:
                    self._registry.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                return
        except OSError as exc:
            action = "enable" if enabled else "disable"
            raise StartupRegistrationError(
                f"Windows could not {action} launch at sign-in: {exc}"
            ) from exc


__all__ = [
    "STARTUP_ARGUMENT",
    "StartupRegistration",
    "StartupRegistrationError",
    "build_startup_command",
]
