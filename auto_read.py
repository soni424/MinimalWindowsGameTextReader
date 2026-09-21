"""Foreground-bound, bounded polling for automatic fixed-box dialogue reading.

This module never touches Tk or speech. It supplies already captured images to
the application's existing OCR worker and accepts corrected text back from it.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from PIL import ImageChops, ImageStat


@dataclass(frozen=True)
class AutoReadPolicy:
    """Bounded timing profile for one Auto-Read speed."""

    speed: str
    poll_interval: float
    settle_delay: float
    confirmation_interval: float
    idle_rescan_interval: float


AUTO_READ_POLICIES = {
    "normal": AutoReadPolicy("normal", 0.35, 0.50, 0.40, 2.0),
    "fast": AutoReadPolicy("fast", 0.12, 0.18, 0.12, 1.0),
}


@dataclass(frozen=True)
class ForegroundWindow:
    handle: int
    process_id: int
    title: str
    bounds: tuple[int, int, int, int]

    def covers(self, box: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = self.bounds
        return left <= box[0] and top <= box[1] and right >= box[2] and bottom >= box[3]


def foreground_window() -> ForegroundWindow | None:
    """Get the actual foreground HWND; a minimized/desktop/app window is ineligible."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, len(title))
        if not title.value or pid.value == os.getpid():
            return None
        return ForegroundWindow(
            int(hwnd), int(pid.value), title.value,
            (rect.left, rect.top, rect.right, rect.bottom),
        )
    except (AttributeError, OSError):
        return None


def window_exists(window: ForegroundWindow) -> bool:
    if os.name != "nt":
        return True
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = (ctypes.c_void_p,)
        return bool(user32.IsWindow(window.handle))
    except (AttributeError, OSError):
        return False


def normalize_dialogue(text: str) -> str:
    """Ignore OCR spacing jitter, but retain punctuation and letter differences."""
    return re.sub(r"\s+", " ", text).strip().casefold()


class DialogueGate:
    """Require two matching OCR results; two blanks re-arm an identical later line."""

    def __init__(self) -> None:
        self.candidate = ""
        self.confirmations = 0
        self.last_spoken = ""

    def accept(self, text: str) -> bool:
        value = normalize_dialogue(text)
        if value != self.candidate:
            self.candidate = value
            self.confirmations = 1
        else:
            self.confirmations += 1
        if self.confirmations < 2:
            return False
        if not value:
            self.last_spoken = ""
            return False
        if value == self.last_spoken:
            return False
        self.last_spoken = value
        return True

    def remember_manual(self, text: str) -> None:
        self.last_spoken = normalize_dialogue(text)
        self.candidate = self.last_spoken
        self.confirmations = 2


class AutoReadWatcher:
    """Capture only a bound foreground game's fixed box, without opening overlays."""

    def __init__(
        self,
        capture: Callable[[list[int]], Any],
        submit: Callable[[Any, tuple[int, int, int, int], int, int], bool],
        on_state: Callable[[bool, str, bool], None],
        foreground: Callable[[], ForegroundWindow | None] = foreground_window,
        exists: Callable[[ForegroundWindow], bool] = window_exists,
        area_valid: Callable[[tuple[int, int, int, int]], bool] | None = None,
        *,
        speed: str = "normal",
        interval: float | None = None,
    ) -> None:
        self.capture = capture
        self.submit = submit
        self.on_state = on_state
        self.foreground = foreground
        self.exists = exists
        self.area_valid = area_valid or (lambda _box: True)
        if speed not in AUTO_READ_POLICIES:
            raise ValueError(f"Unknown Auto-Read speed: {speed!r}")
        self._speed = speed
        self._interval_override = interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.session = 0
        self.active = False
        self.box = (0, 0, 0, 0)
        self.profile = ""
        self.target: ForegroundWindow | None = None
        self.revision = 0
        self.pending = False
        self.gate = DialogueGate()
        self._last_image = None
        self._last_change = 0.0
        self._last_ocr = 0.0
        self._last_scan_revision = -1
        self._errors = 0
        self._retry_after = 0.0

    @property
    def speed(self) -> str:
        with self._lock:
            return self._speed

    @property
    def policy(self) -> AutoReadPolicy:
        with self._lock:
            policy = AUTO_READ_POLICIES[self._speed]
            if self._interval_override is None:
                return policy
            return AutoReadPolicy(
                policy.speed,
                max(0.001, float(self._interval_override)),
                policy.settle_delay,
                policy.confirmation_interval,
                policy.idle_rescan_interval,
            )

    def set_speed(self, speed: str) -> None:
        """Apply a saved timing profile without stopping or rebinding the game."""
        normalised = str(speed).strip().lower()
        if normalised not in AUTO_READ_POLICIES:
            raise ValueError(f"Unknown Auto-Read speed: {speed!r}")
        with self._lock:
            if normalised == self._speed:
                return
            self._speed = normalised
            self._last_change = time.monotonic()
            self._last_scan_revision = -1
            active = self.active
            profile = self.profile
            target = self.target
        if active:
            label = normalised.title()
            if target is None:
                message = f"Auto-Read armed for {profile} ({label} mode); return to the game within 30 seconds."
            else:
                message = f"Auto-reading {profile} in {target.title} ({label} mode)."
            self.on_state(True, message, False)

    def start(self, box: list[int], profile: str) -> None:
        self.stop(notify=False)
        with self._lock:
            self.session += 1
            self.active = True
            self.box = tuple(box)
            self.profile = profile
            self.target = None
            self.revision = 0
            self.pending = False
            self.gate = DialogueGate()
            self._last_image = None
            self._last_change = time.monotonic()
            self._last_ocr = self._last_change
            self._last_scan_revision = -1
            self._errors = 0
            self._retry_after = 0.0
            session = self.session
            self._stop.clear()
        label = self.speed.title()
        self.on_state(True, f"Auto-Read armed for {profile} ({label} mode); return to the game within 30 seconds.", False)
        self._thread = threading.Thread(
            target=self._run, args=(session,), name="auto-read-watcher", daemon=True
        )
        self._thread.start()

    def stop(self, reason: str = "Auto-Read stopped.", *, notify: bool = True) -> None:
        with self._lock:
            was_active = self.active
            self.active = False
            self.session += 1
            self.pending = False
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if notify and was_active:
            self.on_state(False, reason, False)

    def is_current(self, session: int, revision: int) -> bool:
        with self._lock:
            return self.active and self.session == session and self.revision == revision

    def accept_result(self, session: int, revision: int, text: str) -> bool:
        with self._lock:
            if self.active and self.session == session:
                self.pending = False
            if not self.is_current(session, revision):
                return False
            self._errors = 0
            self._retry_after = 0.0
            return self.gate.accept(text)

    def note_error(self, session: int, revision: int, message: str) -> None:
        with self._lock:
            if self.active and self.session == session:
                self.pending = False
            if not self.is_current(session, revision):
                return
            self._errors += 1
            errors = self._errors
            self._retry_after = time.monotonic() + min(2.0 * errors, 5.0)
        if errors >= 3:
            self.stop("Auto-Read stopped after repeated capture/OCR errors.")
            self.on_state(False, f"Auto-Read error: {message}", True)

    def remember_manual(self, text: str) -> None:
        with self._lock:
            if self.active:
                self.gate.remember_manual(text)

    def manual_started(self) -> None:
        """Invalidate an auto OCR job displaced by a higher-priority manual read."""
        with self._lock:
            if self.active:
                self.revision += 1
                self.pending = False
                self._last_scan_revision = -1

    @staticmethod
    def _fingerprint(image: Any) -> Any:
        return image.convert("L").resize((64, 32))

    @staticmethod
    def _scan_due(
        now: float,
        revision: int,
        last_change: float,
        last_ocr: float,
        last_scan_revision: int,
        confirmations: int,
        policy: AutoReadPolicy,
    ) -> bool:
        if now - last_ocr < policy.confirmation_interval:
            return False
        quiet_candidate = (
            now - last_change >= policy.settle_delay
            and (revision != last_scan_revision or confirmations < 2)
        )
        return quiet_candidate or now - last_ocr >= policy.idle_rescan_interval

    def _run(self, session: int) -> None:
        armed_until = time.monotonic() + 30.0
        next_area_check = 0.0
        paused = False
        while True:
            policy = self.policy
            if self._stop.wait(policy.poll_interval):
                return
            with self._lock:
                if not self.active or session != self.session:
                    return
                box = self.box
                target = self.target
                retry_after = self._retry_after
                pending = self.pending
            if time.monotonic() < retry_after:
                continue
            if time.monotonic() >= next_area_check:
                next_area_check = time.monotonic() + 2.0
                try:
                    valid = self.area_valid(box)
                except Exception:
                    valid = False
                if not valid:
                    self.stop("Auto-Read stopped because the saved capture area or display is unavailable.")
                    return
            current = self.foreground()
            if target is None:
                if current is not None and current.covers(box):
                    with self._lock:
                        if session != self.session:
                            return
                        self.target = current
                    self.on_state(True, f"Auto-reading {self.profile} in {current.title} ({self.speed.title()} mode).", False)
                elif time.monotonic() >= armed_until:
                    self.stop("Auto-Read was not started: no game covered the saved box within 30 seconds.")
                    return
                else:
                    continue
            elif not self.exists(target):
                self.stop("Auto-Read stopped because the target game closed.")
                return
            elif current is None or (current.handle, current.process_id) != (target.handle, target.process_id):
                if not paused:
                    self.on_state(True, "Auto-Read paused; return to the target game to resume.", False)
                    paused = True
                continue
            elif not current.covers(box):
                self.stop("Auto-Read stopped because the game no longer covers the saved box.")
                return
            elif paused:
                self.on_state(True, f"Auto-reading {self.profile} in {target.title} ({self.speed.title()} mode).", False)
                paused = False
            if pending:
                continue
            try:
                image = self.capture(list(box))
                fingerprint = self._fingerprint(image)
            except Exception as exc:
                self.note_error(session, self.revision, str(exc))
                continue
            now = time.monotonic()
            with self._lock:
                if session != self.session or not self.active:
                    return
                if self._last_image is None or ImageStat.Stat(
                    ImageChops.difference(fingerprint, self._last_image)
                ).mean[0] >= 0.8:
                    self.revision += 1
                    self._last_change = now
                    self._last_image = fingerprint
                revision = self.revision
                policy = self.policy
                if self.pending:
                    continue
                due = self._scan_due(
                    now,
                    revision,
                    self._last_change,
                    self._last_ocr,
                    self._last_scan_revision,
                    self.gate.confirmations,
                    policy,
                )
                if not due:
                    continue
                self.pending = True
                self._last_ocr = now
                self._last_scan_revision = revision
            try:
                submitted = self.submit(image, box, session, revision)
            except Exception as exc:
                self.note_error(session, revision, str(exc))
                continue
            if not submitted:
                with self._lock:
                    if self.session == session and self.revision == revision:
                        self.pending = False


__all__ = [
    "AUTO_READ_POLICIES",
    "AutoReadPolicy",
    "AutoReadWatcher",
    "DialogueGate",
    "ForegroundWindow",
    "foreground_window",
    "normalize_dialogue",
]
