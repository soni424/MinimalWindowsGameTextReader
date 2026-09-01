"""Theme palettes and Windows appearance helpers for the settings UI."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    """Semantic colours used by both Tk and ttk widgets."""

    name: str
    dark: bool
    window: str
    surface: str
    card: str
    card_alt: str
    input: str
    text: str
    muted: str
    border: str
    accent: str
    accent_hover: str
    accent_pressed: str
    button: str
    button_hover: str
    selection: str
    success: str
    success_soft: str
    danger: str
    danger_soft: str


LIGHT = ThemePalette(
    name="light",
    dark=False,
    window="#eef2f7",
    surface="#f8fafc",
    card="#ffffff",
    card_alt="#e8eef6",
    input="#ffffff",
    text="#172033",
    muted="#637083",
    border="#d8e0ea",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    accent_pressed="#1e40af",
    button="#e7edf5",
    button_hover="#dbe4ef",
    selection="#bfdbfe",
    success="#15803d",
    success_soft="#dcfce7",
    danger="#b91c1c",
    danger_soft="#fee2e2",
)

DARK = ThemePalette(
    name="dark",
    dark=True,
    window="#0b1018",
    surface="#101722",
    card="#151e2b",
    card_alt="#1b2636",
    input="#0d1520",
    text="#edf3fb",
    muted="#9ba9bb",
    border="#2a384b",
    accent="#4f8cff",
    accent_hover="#6ba0ff",
    accent_pressed="#3776e5",
    button="#223044",
    button_hover="#2c3d54",
    selection="#274b7a",
    success="#5ee58a",
    success_soft="#123622",
    danger="#ff7b7b",
    danger_soft="#3b1b20",
)


def system_uses_dark_mode() -> bool:
    """Read the current Windows app-theme preference, defaulting to light."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def resolve_theme(preference: str) -> ThemePalette:
    """Resolve a persisted light/dark/system preference to concrete colours."""
    normalised = preference.strip().lower() if isinstance(preference, str) else "system"
    if normalised == "dark":
        return DARK
    if normalised == "light":
        return LIGHT
    return DARK if system_uses_dark_mode() else LIGHT


def apply_windows_title_bar(root: object, dark: bool) -> None:
    """Best-effort dark title bar on supported Windows 10/11 builds."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        enabled = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:
        pass
