"""Persistent capture regions that remain attached to their Windows display."""

from __future__ import annotations

import copy
import ctypes
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from config import ConfigStore, MAX_PROFILE_NAME_LENGTH


class ProfileError(ValueError):
    """Raised when a capture-profile operation would create invalid state."""


@dataclass(frozen=True)
class MonitorInfo:
    """Physical-pixel monitor bounds and the Windows display-device identity."""

    device: str
    left: int
    top: int
    right: int
    bottom: int
    dpi_x: int = 96
    dpi_y: int = 96
    primary: bool = False

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def bounds(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


@dataclass(frozen=True)
class CaptureAreaResolution:
    """Result of mapping one saved profile onto the current monitor layout."""

    box: list[int] | None
    available: bool
    reason: str = ""


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def get_monitors() -> list[MonitorInfo]:
    """Enumerate current displays in the same physical coordinate space as screenshots."""

    monitors: list[MonitorInfo] = []
    try:
        user32 = ctypes.windll.user32
        callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        @callback_type(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        def callback(handle: int, _dc: int, _rect: object, _data: int) -> bool:
            info = _MonitorInfoExW()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                return True
            dpi_x = ctypes.c_uint(96)
            dpi_y = ctypes.c_uint(96)
            try:
                # MDT_EFFECTIVE_DPI follows the scaling Windows applies to this display.
                ctypes.windll.shcore.GetDpiForMonitor(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            except Exception:
                pass
            rect = info.rcMonitor
            monitors.append(
                MonitorInfo(
                    str(info.szDevice),
                    int(rect.left),
                    int(rect.top),
                    int(rect.right),
                    int(rect.bottom),
                    int(dpi_x.value),
                    int(dpi_y.value),
                    bool(info.dwFlags & 1),
                )
            )
            return True

        if not user32.EnumDisplayMonitors(None, None, callback, 0):
            monitors.clear()
    except Exception:
        monitors.clear()
    if monitors:
        return monitors
    try:
        user32 = ctypes.windll.user32
        return [MonitorInfo("primary", 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), 96, 96, True)]
    except Exception:
        return [MonitorInfo("primary", 0, 0, 1920, 1080, 96, 96, True)]


def _intersection_area(box: list[int], monitor: MonitorInfo) -> int:
    width = max(0, min(box[2], monitor.right) - max(box[0], monitor.left))
    height = max(0, min(box[3], monitor.bottom) - max(box[1], monitor.top))
    return width * height


def _valid_box(value: object) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (int(part) for part in value)
    except (TypeError, ValueError):
        return None
    return [left, top, right, bottom] if right > left and bottom > top else None


def build_capture_area(box: list[int], monitors: list[MonitorInfo] | None = None) -> dict[str, Any]:
    """Record absolute and monitor-relative coordinates for a new profile area."""

    valid = _valid_box(box)
    if valid is None:
        raise ProfileError("The capture area must have positive width and height.")
    monitors = list(monitors if monitors is not None else get_monitors())
    target = max(monitors, key=lambda item: _intersection_area(valid, item), default=None)
    if target is None or _intersection_area(valid, target) == 0 or target.width <= 0 or target.height <= 0:
        return {
            "box": valid,
            "monitor_device": "",
            "monitor_bounds": None,
            "monitor_dpi": None,
            "relative_box": None,
        }
    relative = [
        (valid[0] - target.left) / target.width,
        (valid[1] - target.top) / target.height,
        (valid[2] - target.left) / target.width,
        (valid[3] - target.top) / target.height,
    ]
    return {
        "box": valid,
        "monitor_device": target.device,
        "monitor_bounds": target.bounds,
        "monitor_dpi": [target.dpi_x, target.dpi_y],
        "relative_box": [round(value, 10) for value in relative],
    }


def _box_is_on_current_desktop(box: list[int], monitors: list[MonitorInfo]) -> bool:
    points = ((box[0], box[1]), (box[2] - 1, box[1]), (box[0], box[3] - 1), (box[2] - 1, box[3] - 1))
    return all(any(monitor.left <= x < monitor.right and monitor.top <= y < monitor.bottom for monitor in monitors) for x, y in points)


def resolve_capture_area(area: Mapping[str, Any] | None, monitors: list[MonitorInfo] | None = None) -> CaptureAreaResolution:
    """Map a saved area to the same connected display, or return a safe failure."""

    if not isinstance(area, Mapping):
        return CaptureAreaResolution(None, False, "No capture area is saved for this profile.")
    box = _valid_box(area.get("box"))
    if box is None:
        return CaptureAreaResolution(None, False, "The saved capture area is invalid.")
    monitors = list(monitors if monitors is not None else get_monitors())
    device = area.get("monitor_device")
    relative = area.get("relative_box")
    if not isinstance(device, str) or not device or not isinstance(relative, (list, tuple)) or len(relative) != 4:
        if _box_is_on_current_desktop(box, monitors):
            return CaptureAreaResolution(box, True)
        return CaptureAreaResolution(None, False, "The saved capture area is outside the connected displays. Edit the area to restore it.")

    target = next((monitor for monitor in monitors if monitor.device.casefold() == device.casefold()), None)
    if target is None:
        return CaptureAreaResolution(None, False, f"The saved monitor {device} is not connected. Select another profile or edit its area.")
    try:
        left = target.left + round(float(relative[0]) * target.width)
        top = target.top + round(float(relative[1]) * target.height)
        right = target.left + round(float(relative[2]) * target.width)
        bottom = target.top + round(float(relative[3]) * target.height)
    except (TypeError, ValueError, OverflowError):
        return CaptureAreaResolution(None, False, "The saved monitor-relative capture area is invalid.")
    resolved = _valid_box([left, top, right, bottom])
    if resolved is None or _intersection_area(resolved, target) == 0:
        return CaptureAreaResolution(None, False, "The saved capture area no longer overlaps its display. Edit the area to restore it.")
    return CaptureAreaResolution(resolved, True)


class CaptureProfileManager:
    """CRUD and selection boundary for profile settings stored in ``ConfigStore``."""

    def __init__(self, config: ConfigStore, monitor_provider: Callable[[], list[MonitorInfo]] = get_monitors) -> None:
        self.config = config
        self._monitor_provider = monitor_provider

    @property
    def profiles(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.config.get()["capture_profiles"])

    @property
    def selected(self) -> dict[str, Any]:
        settings = self.config.get()
        selected_id = settings["selected_profile_id"]
        return copy.deepcopy(next(profile for profile in settings["capture_profiles"] if profile["id"] == selected_id))

    def _save(self, profiles: list[dict[str, Any]], selected_id: str) -> dict[str, Any]:
        settings = self.config.get()
        settings["capture_profiles"] = profiles
        settings["selected_profile_id"] = selected_id
        return self.config.save(settings)

    def _checked_name(self, requested: str, ignore_id: str = "") -> str:
        name = str(requested).strip()[:MAX_PROFILE_NAME_LENGTH]
        if not name:
            raise ProfileError("Enter a profile name.")
        if any(profile["id"] != ignore_id and profile["name"].casefold() == name.casefold() for profile in self.profiles):
            raise ProfileError(f'A profile named "{name}" already exists.')
        return name

    def create(self, name: str) -> dict[str, Any]:
        name = self._checked_name(name)
        profiles = self.profiles
        profile = {"id": uuid.uuid4().hex, "name": name, "capture_area": None, "settings": {}}
        profiles.append(profile)
        saved = self._save(profiles, profile["id"])
        return copy.deepcopy(next(item for item in saved["capture_profiles"] if item["id"] == profile["id"]))

    def rename(self, profile_id: str, name: str) -> dict[str, Any]:
        name = self._checked_name(name, profile_id)
        settings = self.config.get()
        found = False
        for profile in settings["capture_profiles"]:
            if profile["id"] == profile_id:
                profile["name"] = name
                found = True
                break
        if not found:
            raise ProfileError("That capture profile no longer exists.")
        saved = self._save(settings["capture_profiles"], settings["selected_profile_id"])
        return copy.deepcopy(next(item for item in saved["capture_profiles"] if item["id"] == profile_id))

    def delete(self, profile_id: str) -> None:
        settings = self.config.get()
        profiles = settings["capture_profiles"]
        if len(profiles) <= 1:
            raise ProfileError("At least one capture profile must remain.")
        remaining = [profile for profile in profiles if profile["id"] != profile_id]
        if len(remaining) == len(profiles):
            raise ProfileError("That capture profile no longer exists.")
        selected_id = settings["selected_profile_id"]
        if selected_id == profile_id:
            selected_id = remaining[0]["id"]
        self._save(remaining, selected_id)

    def select(self, profile_id: str) -> dict[str, Any]:
        profiles = self.profiles
        if profile_id not in {profile["id"] for profile in profiles}:
            raise ProfileError("That capture profile no longer exists.")
        saved = self._save(profiles, profile_id)
        return copy.deepcopy(next(item for item in saved["capture_profiles"] if item["id"] == profile_id))

    def update_selected_area(self, box: list[int]) -> dict[str, Any]:
        settings = self.config.get()
        selected_id = settings["selected_profile_id"]
        area = build_capture_area(box, self._monitor_provider())
        for profile in settings["capture_profiles"]:
            if profile["id"] == selected_id:
                profile["capture_area"] = area
                break
        saved = self._save(settings["capture_profiles"], selected_id)
        return copy.deepcopy(next(item for item in saved["capture_profiles"] if item["id"] == selected_id))

    def resolve_selected(self) -> CaptureAreaResolution:
        return resolve_capture_area(self.selected.get("capture_area"), self._monitor_provider())
