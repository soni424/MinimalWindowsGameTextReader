"""Validated Windows global shortcuts with real OS conflict detection."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Final


class HotkeyError(ValueError):
    """Raised for malformed, duplicate, reserved, or unavailable shortcuts."""


_MODIFIERS: Final[dict[str, tuple[str, str, int]]] = {
    "ctrl": ("Ctrl", "<ctrl>", 0x0002),
    "control": ("Ctrl", "<ctrl>", 0x0002),
    "alt": ("Alt", "<alt>", 0x0001),
    "shift": ("Shift", "<shift>", 0x0004),
    "win": ("Win", "<cmd>", 0x0008),
    "windows": ("Win", "<cmd>", 0x0008),
}
_MODIFIER_ORDER: Final[dict[str, int]] = {"Ctrl": 0, "Alt": 1, "Shift": 2, "Win": 3}
_SPECIAL_KEYS: Final[dict[str, tuple[str, str, int]]] = {
    "space": ("Space", "<space>", 0x20),
    "enter": ("Enter", "<enter>", 0x0D),
    "return": ("Enter", "<enter>", 0x0D),
    "tab": ("Tab", "<tab>", 0x09),
    "escape": ("Esc", "<esc>", 0x1B),
    "esc": ("Esc", "<esc>", 0x1B),
    "insert": ("Insert", "<insert>", 0x2D),
    "delete": ("Delete", "<delete>", 0x2E),
    "home": ("Home", "<home>", 0x24),
    "end": ("End", "<end>", 0x23),
    "pageup": ("PageUp", "<page_up>", 0x21),
    "prior": ("PageUp", "<page_up>", 0x21),
    "pagedown": ("PageDown", "<page_down>", 0x22),
    "next": ("PageDown", "<page_down>", 0x22),
    "up": ("Up", "<up>", 0x26),
    "down": ("Down", "<down>", 0x28),
    "left": ("Left", "<left>", 0x25),
    "right": ("Right", "<right>", 0x27),
}
_PUNCTUATION_VK: Final[dict[str, int]] = {
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


@dataclass(frozen=True)
class _ParsedHotkey:
    display: str
    pynput: str
    modifiers: int
    virtual_key: int


def _compact_key_name(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "")


def _parse_hotkey(value: str, allow_empty: bool = False) -> _ParsedHotkey | None:
    if not isinstance(value, str):
        raise HotkeyError("A shortcut must be text.")
    if not value.strip():
        if allow_empty:
            return None
        raise HotkeyError("A shortcut cannot be empty.")
    parts = [part.strip() for part in value.split("+")]
    if any(not part for part in parts):
        raise HotkeyError("Use one trigger key and separate keys with +.")

    display_modifiers: list[str] = []
    modifier_flags = 0
    triggers: list[tuple[str, str, int]] = []
    for part in parts:
        compact = _compact_key_name(part)
        modifier = _MODIFIERS.get(compact)
        if modifier:
            display, _pynput_name, flag = modifier
            if display in display_modifiers:
                raise HotkeyError("A key can only appear once in a shortcut.")
            display_modifiers.append(display)
            modifier_flags |= flag
            continue
        special = _SPECIAL_KEYS.get(compact)
        if special:
            triggers.append(special)
            continue
        if compact.startswith("f") and compact[1:].isdigit() and 1 <= int(compact[1:]) <= 24:
            number = int(compact[1:])
            if number == 12:
                raise HotkeyError("F12 is reserved by Windows and cannot be used as a global shortcut.")
            triggers.append((f"F{number}", f"<f{number}>", 0x70 + number - 1))
            continue
        if len(part) == 1 and part.isascii() and part.isalnum():
            upper = part.upper()
            triggers.append((upper, upper.lower(), ord(upper)))
            continue
        if len(part) == 1 and part in _PUNCTUATION_VK:
            triggers.append((part, part, _PUNCTUATION_VK[part]))
            continue
        raise HotkeyError(f"Unsupported shortcut key: {part!r}.")

    if len(triggers) != 1:
        raise HotkeyError("A shortcut needs exactly one letter, number, function, or navigation key.")
    trigger_display, trigger_pynput, virtual_key = triggers[0]
    if not display_modifiers and not trigger_display.startswith("F"):
        raise HotkeyError("Add Ctrl, Alt, Shift, or Windows so normal typing is not intercepted.")
    display_modifiers.sort(key=_MODIFIER_ORDER.get)
    pynput_by_display = {value[0]: value[1] for value in _MODIFIERS.values()}
    display = "+".join([*display_modifiers, trigger_display])
    pynput = "+".join([*(pynput_by_display[item] for item in display_modifiers), trigger_pynput])
    return _ParsedHotkey(display, pynput, modifier_flags, virtual_key)


def normalise_hotkey(value: str, allow_empty: bool = False) -> str:
    """Return a friendly canonical representation suitable for settings storage."""
    parsed = _parse_hotkey(value, allow_empty=allow_empty)
    return parsed.display if parsed else ""


def to_pynput_hotkey(value: str) -> str:
    """Return pynput syntax for compatibility with older callers."""
    parsed = _parse_hotkey(value)
    assert parsed is not None
    return parsed.pynput


class _NativeHotkeyThread(threading.Thread):
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_NOREPEAT = 0x4000

    def __init__(self, bindings: dict[int, tuple[_ParsedHotkey, Callable[[], None]]]) -> None:
        super().__init__(name="global-hotkeys", daemon=True)
        self.bindings = bindings
        self.ready = threading.Event()
        self.error: str | None = None
        self.thread_id = 0

    def run(self) -> None:
        if os.name != "nt":
            self.error = "Global shortcuts are only supported on Windows."
            self.ready.set()
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.thread_id = int(kernel32.GetCurrentThreadId())
        registered: list[int] = []
        try:
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            for identifier, (parsed, _callback) in self.bindings.items():
                if not user32.RegisterHotKey(None, identifier, parsed.modifiers | self.MOD_NOREPEAT, parsed.virtual_key):
                    error_code = ctypes.get_last_error()
                    if error_code == 1409:
                        self.error = f"{parsed.display} is already assigned by this app or another program."
                    else:
                        self.error = f"Windows could not register {parsed.display} (error {error_code})."
                    return
                registered.append(identifier)
            self.ready.set()
            while True:
                status = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status <= 0:
                    break
                if message.message == self.WM_HOTKEY:
                    binding = self.bindings.get(int(message.wParam))
                    if binding:
                        try:
                            binding[1]()
                        except Exception:
                            pass
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)
            self.ready.set()

    def request_stop(self) -> None:
        if self.thread_id and os.name == "nt":
            try:
                ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass


class HotkeyManager:
    """Own Windows hotkey registrations and replace them without leaking hooks."""

    def __init__(
        self,
        on_fixed: Callable[[], None],
        on_snippet: Callable[[], None],
        on_read_again: Callable[[], None] | None = None,
    ) -> None:
        self._callbacks = (on_fixed, on_snippet, on_read_again or (lambda: None))
        self._listener: _NativeHotkeyThread | None = None
        self._bindings: tuple[_ParsedHotkey | None, ...] = (None, None, None)
        self._lock = threading.RLock()
        self.is_running = False

    def _start(self, parsed_bindings: tuple[_ParsedHotkey | None, ...]) -> None:
        active = {
            index + 1: (parsed, self._callbacks[index])
            for index, parsed in enumerate(parsed_bindings)
            if parsed is not None
        }
        if not active:
            self._listener = None
            self._bindings = parsed_bindings
            self.is_running = False
            return
        listener = _NativeHotkeyThread(active)
        listener.start()
        if not listener.ready.wait(3):
            listener.request_stop()
            raise HotkeyError("Windows did not respond while registering the shortcuts.")
        if listener.error:
            listener.join(timeout=1)
            raise HotkeyError(listener.error)
        self._listener = listener
        self._bindings = parsed_bindings
        self.is_running = True

    def apply(self, fixed: str, snippet: str) -> tuple[str, str]:
        """Validate and register zero, one, or two shortcuts; empty means disabled."""
        fixed_key, snippet_key, _read_again_key = self._apply_values(fixed, snippet, "")
        return fixed_key, snippet_key

    def apply_all(self, fixed: str, snippet: str, read_again: str) -> tuple[str, str, str]:
        """Validate and register all supported shortcuts as one atomic set."""
        return self._apply_values(fixed, snippet, read_again)

    def _apply_values(self, fixed: str, snippet: str, read_again: str) -> tuple[str, str, str]:
        parsed = (
            _parse_hotkey(fixed, allow_empty=True),
            _parse_hotkey(snippet, allow_empty=True),
            _parse_hotkey(read_again, allow_empty=True),
        )
        active = [item.display for item in parsed if item is not None]
        if len(set(active)) != len(active):
            raise HotkeyError("Read Fixed Box, Select a Snippet, and Read Again must use different shortcuts.")
        requested = parsed
        with self._lock:
            previous = self._bindings
            self.stop()
            try:
                self._start(requested)
            except Exception:
                try:
                    self._start(previous)
                except Exception:
                    self._listener = None
                    self.is_running = False
                raise
        return tuple(item.display if item else "" for item in parsed)  # type: ignore[return-value]

    def stop(self) -> None:
        """Unregister all shortcuts and stop the native Windows message loop."""
        with self._lock:
            listener, self._listener = self._listener, None
            self.is_running = False
            if listener is not None:
                listener.request_stop()
                listener.join(timeout=2)
