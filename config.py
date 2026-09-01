"""Persistent settings for Minimal Windows Game Text Reader.

The configuration deliberately stays in a small, human-readable JSON file next
to the application.  Values read from disk are validated so a manually edited
or partially-written file cannot prevent the app from starting.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Mapping


APP_DIRECTORY = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIRECTORY / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "theme": "system",
    "voice": "",
    "rate": 0,
    "volume": 100,
    "speech": {
        "capture_mode": "replace",
    },
    "fixed_box": None,
    "window": {
        "width": 980,
        "height": 860,
        "x": None,
        "y": None,
        "state": "normal",
    },
    "capture_profiles": [
        {
            "id": "default",
            "name": "Default",
            "capture_area": None,
            "settings": {},
        }
    ],
    "selected_profile_id": "default",
    "hotkeys": {
        "fixed": "Alt+Z",
        "snippet": "Alt+S",
        "read_again": "",
    },
    "ocr": {
        "enabled": True,
        "strength": "conservative",
        "debug_logging": False,
        "replacements": [],
        "protected_words": [],
    },
}

MAX_REPLACEMENT_RULES = 250
MAX_PROTECTED_WORDS = 500
MAX_TERM_LENGTH = 200
MAX_REPLACEMENT_LENGTH = 500
MAX_CAPTURE_PROFILES = 100
MAX_PROFILE_NAME_LENGTH = 80


def _nonempty_string(value: Any, default: str, maximum: int) -> str:
    """Return a trimmed, bounded string or a known-good default."""
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()[:maximum]


def _hotkey_string(value: Any, default: str) -> str:
    """Preserve an intentional empty shortcut while rejecting malformed blanks."""
    if value == "":
        return ""
    return _nonempty_string(value, default, 100)


def _normalise_theme(value: Any) -> str:
    """Return one of the supported persisted appearance preferences."""
    theme = value.strip().lower() if isinstance(value, str) else ""
    return theme if theme in {"system", "light", "dark"} else DEFAULT_CONFIG["theme"]


def _normalise_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _normalise_replacements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    rules: list[dict[str, Any]] = []
    seen: set[tuple[str, bool, bool]] = set()
    for raw_rule in value[:MAX_REPLACEMENT_RULES]:
        if not isinstance(raw_rule, Mapping):
            continue
        original = raw_rule.get("original")
        replacement = raw_rule.get("replacement")
        if not isinstance(original, str) or not original.strip() or not isinstance(replacement, str):
            continue
        original = original.strip()[:MAX_TERM_LENGTH]
        replacement = replacement[:MAX_REPLACEMENT_LENGTH]
        case_sensitive = _normalise_bool(raw_rule.get("case_sensitive"), False)
        whole_word = _normalise_bool(raw_rule.get("whole_word"), True)
        identity = (original if case_sensitive else original.casefold(), case_sensitive, whole_word)
        if identity in seen:
            continue
        seen.add(identity)
        rules.append(
            {
                "original": original,
                "replacement": replacement,
                "enabled": _normalise_bool(raw_rule.get("enabled"), True),
                "case_sensitive": case_sensitive,
                "whole_word": whole_word,
            }
        )
    return rules


def _normalise_protected_words(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    protected: list[str] = []
    seen: set[str] = set()
    for item in value[:MAX_PROTECTED_WORDS]:
        if not isinstance(item, str) or not item.strip():
            continue
        term = item.strip()[:MAX_TERM_LENGTH]
        identity = term.casefold()
        if identity not in seen:
            seen.add(identity)
            protected.append(term)
    return protected


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """Return *value* as a bounded integer, or *default* if it is invalid."""
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _normalise_box(value: Any) -> list[int] | None:
    """Validate a screen bounding box and return it in left/top/right/bottom order."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (int(part) for part in value)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _normalise_optional_coordinate(value: Any) -> int | None:
    if value is None:
        return None
    try:
        coordinate = int(value)
    except (TypeError, ValueError):
        return None
    return max(-100_000, min(100_000, coordinate))


def _normalise_window(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    state = raw.get("state", DEFAULT_CONFIG["window"]["state"])
    state = state.strip().lower() if isinstance(state, str) else "normal"
    if state not in {"normal", "maximized"}:
        state = "normal"
    return {
        "width": _clamp_int(raw.get("width"), 480, 10_000, DEFAULT_CONFIG["window"]["width"]),
        "height": _clamp_int(raw.get("height"), 420, 10_000, DEFAULT_CONFIG["window"]["height"]),
        "x": _normalise_optional_coordinate(raw.get("x")),
        "y": _normalise_optional_coordinate(raw.get("y")),
        "state": state,
    }


def _normalise_relative_box(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(part) and -10.0 <= part <= 10.0 for part in (left, top, right, bottom)):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _normalise_capture_area(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    box = _normalise_box(value.get("box"))
    if box is None:
        return None
    device = value.get("monitor_device", "")
    device = device.strip()[:260] if isinstance(device, str) else ""
    bounds = _normalise_box(value.get("monitor_bounds"))
    dpi = value.get("monitor_dpi")
    if isinstance(dpi, (list, tuple)) and len(dpi) == 2:
        dpi_value = [
            _clamp_int(dpi[0], 48, 960, 96),
            _clamp_int(dpi[1], 48, 960, 96),
        ]
    else:
        dpi_value = None
    relative = _normalise_relative_box(value.get("relative_box"))
    if not device or bounds is None or relative is None:
        device, bounds, dpi_value, relative = "", None, None, None
    return {
        "box": box,
        "monitor_device": device,
        "monitor_bounds": bounds,
        "monitor_dpi": dpi_value,
        "relative_box": relative,
    }


def _normalise_profiles(value: Any, legacy_box: list[int] | None) -> list[dict[str, Any]]:
    raw_profiles = value if isinstance(value, (list, tuple)) else []
    profiles: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    names: set[str] = set()
    for index, raw_profile in enumerate(raw_profiles[:MAX_CAPTURE_PROFILES]):
        if not isinstance(raw_profile, Mapping):
            continue
        identifier = raw_profile.get("id")
        identifier = identifier.strip()[:80] if isinstance(identifier, str) else ""
        if not identifier or identifier in identifiers:
            continue
        requested_name = raw_profile.get("name")
        name = requested_name.strip()[:MAX_PROFILE_NAME_LENGTH] if isinstance(requested_name, str) else ""
        if not name:
            name = f"Profile {index + 1}"
        base_name = name
        suffix = 2
        while name.casefold() in names:
            name = f"{base_name[:MAX_PROFILE_NAME_LENGTH - len(str(suffix)) - 3]} ({suffix})"
            suffix += 1
        raw_area = raw_profile.get("capture_area")
        if raw_area is None and raw_profile.get("box") is not None:
            raw_area = {"box": raw_profile.get("box")}
        settings = raw_profile.get("settings")
        profiles.append(
            {
                "id": identifier,
                "name": name,
                "capture_area": _normalise_capture_area(raw_area),
                # Reserved for future per-profile OCR/TTS settings. Unknown
                # values are deliberately not activated by this release.
                "settings": copy.deepcopy(dict(settings)) if isinstance(settings, Mapping) else {},
            }
        )
        identifiers.add(identifier)
        names.add(name.casefold())
    if not profiles:
        profiles.append(
            {
                "id": "default",
                "name": "Default",
                "capture_area": _normalise_capture_area({"box": legacy_box}) if legacy_box else None,
                "settings": {},
            }
        )
    return profiles


def validate_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge *raw* with defaults and normalise every persisted setting."""
    raw = raw if isinstance(raw, Mapping) else {}
    raw_hotkeys = raw.get("hotkeys") if isinstance(raw.get("hotkeys"), Mapping) else {}
    raw_ocr = raw.get("ocr") if isinstance(raw.get("ocr"), Mapping) else {}
    raw_speech = raw.get("speech") if isinstance(raw.get("speech"), Mapping) else {}
    capture_mode = raw_speech.get("capture_mode", DEFAULT_CONFIG["speech"]["capture_mode"])
    capture_mode = capture_mode.strip().lower() if isinstance(capture_mode, str) else DEFAULT_CONFIG["speech"]["capture_mode"]
    if capture_mode not in {"queue", "replace"}:
        capture_mode = DEFAULT_CONFIG["speech"]["capture_mode"]
    strength = raw_ocr.get("strength", DEFAULT_CONFIG["ocr"]["strength"])
    strength = strength.strip().lower() if isinstance(strength, str) else DEFAULT_CONFIG["ocr"]["strength"]
    if strength not in {"conservative", "balanced", "strong"}:
        strength = DEFAULT_CONFIG["ocr"]["strength"]
    legacy_box = _normalise_box(raw.get("fixed_box"))
    profiles = _normalise_profiles(raw.get("capture_profiles"), legacy_box)
    selected_profile_id = raw.get("selected_profile_id")
    selected_profile_id = selected_profile_id.strip()[:80] if isinstance(selected_profile_id, str) else ""
    if selected_profile_id not in {profile["id"] for profile in profiles}:
        selected_profile_id = profiles[0]["id"]
    selected_profile = next(profile for profile in profiles if profile["id"] == selected_profile_id)
    selected_area = selected_profile.get("capture_area")
    compatible_box = selected_area.get("box") if isinstance(selected_area, Mapping) else None
    return {
        "theme": _normalise_theme(raw.get("theme")),
        "voice": _nonempty_string(raw.get("voice"), DEFAULT_CONFIG["voice"], 1024),
        "rate": _clamp_int(raw.get("rate"), -10, 10, DEFAULT_CONFIG["rate"]),
        "volume": _clamp_int(raw.get("volume"), 0, 100, DEFAULT_CONFIG["volume"]),
        "speech": {
            "capture_mode": capture_mode,
        },
        # Keep the legacy key as a compatibility mirror while profiles remain
        # the authoritative capture-area model.
        "fixed_box": compatible_box,
        "window": _normalise_window(raw.get("window")),
        "capture_profiles": profiles,
        "selected_profile_id": selected_profile_id,
        "hotkeys": {
            "fixed": _hotkey_string(raw_hotkeys.get("fixed"), DEFAULT_CONFIG["hotkeys"]["fixed"]),
            "snippet": _hotkey_string(raw_hotkeys.get("snippet"), DEFAULT_CONFIG["hotkeys"]["snippet"]),
            "read_again": _hotkey_string(raw_hotkeys.get("read_again"), DEFAULT_CONFIG["hotkeys"]["read_again"]),
        },
        "ocr": {
            "enabled": _normalise_bool(raw_ocr.get("enabled"), DEFAULT_CONFIG["ocr"]["enabled"]),
            "strength": strength,
            "debug_logging": _normalise_bool(raw_ocr.get("debug_logging"), DEFAULT_CONFIG["ocr"]["debug_logging"]),
            "replacements": _normalise_replacements(raw_ocr.get("replacements")),
            "protected_words": _normalise_protected_words(raw_ocr.get("protected_words")),
        },
    }


class ConfigStore:
    """Thread-safe read/write access to the application's JSON configuration."""

    def __init__(self, path: str | Path = CONFIG_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)

    def load(self) -> dict[str, Any]:
        """Load settings from disk, preserving usable defaults after malformed JSON."""
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    disk_value = json.load(handle)
            except FileNotFoundError:
                disk_value = {}
            except (OSError, json.JSONDecodeError):
                disk_value = {}
            self._data = validate_config(disk_value)
            return copy.deepcopy(self._data)

    def get(self) -> dict[str, Any]:
        """Return a safe copy of the current in-memory settings."""
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Atomically save settings and return the normalised value that was written."""
        with self._lock:
            if data is not None:
                self._data = validate_config(data)
            else:
                self._data = validate_config(self._data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(self._data, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            return copy.deepcopy(self._data)

    def update(self, **changes: Any) -> dict[str, Any]:
        """Merge top-level changes into settings, save them, and return a copy."""
        with self._lock:
            updated = self.get()
            for key, value in changes.items():
                if key in {"hotkeys", "ocr", "speech"} and isinstance(value, Mapping):
                    updated[key].update(value)
                else:
                    updated[key] = value
            # Older callers still update ``fixed_box`` directly. Mirror that
            # operation into the selected profile so upgrades remain lossless.
            if "fixed_box" in changes and "capture_profiles" not in changes:
                box = _normalise_box(changes.get("fixed_box"))
                selected_id = updated.get("selected_profile_id")
                for profile in updated.get("capture_profiles", []):
                    if profile.get("id") == selected_id:
                        profile["capture_area"] = (
                            {
                                "box": box,
                                "monitor_device": "",
                                "monitor_bounds": None,
                                "monitor_dpi": None,
                                "relative_box": None,
                            }
                            if box
                            else None
                        )
                        break
            return self.save(updated)
