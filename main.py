"""Application entry point for Minimal Windows Game Text Reader."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from queue import Empty, SimpleQueue
from typing import Callable

from PIL import ImageGrab

from app_resources import apply_window_icon
from appearance import flush_windows_compositor
from capture_pipeline import CaptureJob, CaptureWorker, PipelineTimings
from capture_profiles import CaptureProfileManager
from config import ConfigStore
from hotkey_manager import HotkeyManager
from ocr_correction import CorrectionOptions, CorrectionResult, OcrCorrector
from ocr_engine import OcrEngine, OcrError
from overlay import BoxEditorOverlay, QuickSnippetOverlay
from reader_state import ReaderTextState
from settings_ui import SettingsUI
from tray_app import TrayApp
from tts_engine import TtsEngine


def enable_dpi_awareness() -> None:
    """Ask Windows for Per-Monitor V2 physical coordinates before creating Tk."""
    try:
        # Per-Monitor V2 keeps each top-level overlay in the monitor's physical
        # coordinate space when displays use different scaling factors.
        context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
            return
    except Exception:
        pass
    try:
        # V1 is retained for older Windows builds that do not expose the V2 API.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def set_windows_app_identity() -> None:
    """Give Windows a stable identity for taskbar grouping and icon selection."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "GameTextReader.Desktop"
        )
    except Exception:
        pass


class GameTextReaderApplication:
    """Coordinate GUI, tray, hotkeys, native OCR, and queued speech synthesis."""

    def __init__(self) -> None:
        self.config = ConfigStore()
        settings = self.config.load()
        self.root = tk.Tk()
        apply_window_icon(self.root)
        self._closed = False
        self._scheduled_actions: SimpleQueue[Callable[[], None]] = SimpleQueue()
        self._overlay: BoxEditorOverlay | QuickSnippetOverlay | None = None
        self.profiles = CaptureProfileManager(self.config)
        self.text_state = ReaderTextState()
        self._timing_lock = threading.Lock()
        self._pending_speech_timing: tuple[str, PipelineTimings] | None = None
        self.last_performance: dict[str, float] = {}
        self.ocr = OcrEngine()
        self.corrector = OcrCorrector()
        self.tts = TtsEngine(
            on_error=self._speech_error,
            on_started_with_id=self._speech_started,
            on_finished_with_id=self._speech_finished,
            initial_voice_id=settings["voice"],
            initial_capture_mode=settings.get("speech", {}).get("capture_mode", "replace"),
            initial_max_overlap=settings.get("speech", {}).get("max_overlap", 2),
        )
        self.capture_worker = CaptureWorker(
            capture=self._grab_screen,
            recognise=self.ocr.recognise,
            correct=lambda raw, snapshot: self.process_ocr_text(raw, snapshot.get("ocr", {})),
            on_result=self._capture_succeeded,
            on_error=self._capture_failed,
            on_start=self.ocr.warm_up,
            on_close=self.ocr.close,
        )
        self.hotkeys = HotkeyManager(
            on_fixed=self._fixed_hotkey_received,
            on_snippet=lambda: self._schedule(self.open_quick_snippet),
            on_read_again=lambda: self._schedule(self.read_again),
        )
        self.ui = SettingsUI(
            self.root,
            self.config,
            self.tts,
            on_draw_box=self.open_box_editor,
            on_read_box=lambda: self.read_fixed_box(hide_settings=True),
            on_quick_snippet=lambda: self.open_quick_snippet(restore_settings_after=True),
            on_apply_hotkeys=self.apply_hotkeys,
            on_ocr_settings_changed=self.preload_correction_engine,
            on_shortcut_recording=self.set_shortcuts_paused,
            on_read_again=self.read_again,
            on_clear_text=self.clear_text_history,
            on_profile_create=self.create_capture_profile,
            on_profile_rename=self.rename_capture_profile,
            on_profile_delete=self.delete_capture_profile,
            on_profile_select=self.select_capture_profile,
        )
        self.tray = TrayApp(
            on_show=lambda: self._schedule(self.show_window),
            on_read_fixed=lambda: self._schedule(lambda: self.read_fixed_box(hide_settings=True)),
            on_quick_snippet=lambda: self._schedule(self.open_quick_snippet),
            on_stop_speech=lambda: self._schedule(self.stop_speech),
            on_quit=lambda: self._schedule(self.quit_app),
            on_hide=lambda: self._schedule(self.hide_window),
            on_read_again=lambda: self._schedule(self.read_again),
        )
        self.root.protocol("WM_DELETE_WINDOW", self.minimize_window)
        self._apply_initial_hotkeys()
        self.tray.start()
        self.root.after(20, self._drain_scheduled_actions)
        self.preload_correction_engine()
        self._refresh_profiles_ui()

    def _schedule(self, callback: Callable[[], None]) -> None:
        """Queue work from hook, tray, and worker threads for Tk's main thread."""
        if self._closed:
            return
        self._scheduled_actions.put(callback)

    def _drain_scheduled_actions(self) -> None:
        """Run queued callbacks from Tk's event loop without touching Tk off-thread."""
        if self._closed:
            return
        try:
            while True:
                callback = self._scheduled_actions.get_nowait()
                callback()
        except Empty:
            pass
        except tk.TclError:
            pass
        finally:
            if not self._closed:
                try:
                    self.root.after(20, self._drain_scheduled_actions)
                except tk.TclError:
                    pass

    def _speech_error(self, message: str) -> None:
        self.text_state.end_speech()
        self._schedule(lambda: self.ui.set_status(f"Speech error: {message}", error=True))

    def _fixed_hotkey_received(self) -> None:
        """Timestamp the native callback before handing it to Tk's event queue."""
        received_at = time.perf_counter()
        self._schedule(lambda: self.read_fixed_box(hide_settings=True, requested_at=received_at))

    def _speech_started(self, request_id: int, text: str, started_at: float) -> None:
        self.text_state.begin_speech(text, request_id)
        timing: PipelineTimings | None = None
        with self._timing_lock:
            pending = self._pending_speech_timing
            if pending is not None and pending[0] == text:
                timing = pending[1]
                self._pending_speech_timing = None
        if timing is None:
            return
        total_ms = max(0.0, (started_at - timing.requested_at) * 1000.0)
        handoff_ms = max(0.0, (started_at - timing.correction_finished_at) * 1000.0)
        measured = {
            "dispatch_ms": timing.dispatch_ms,
            "capture_ms": timing.capture_ms,
            "ocr_ms": timing.ocr_ms,
            "correction_ms": timing.correction_ms,
            "speech_start_ms": total_ms,
            "speech_handoff_ms": handoff_ms,
        }
        with self._timing_lock:
            self.last_performance = measured
        self._schedule(
            lambda: self.ui.set_status(
                f"Speech started in {total_ms:.0f} ms • handoff {handoff_ms:.0f} ms • capture {timing.capture_ms:.0f} • OCR {timing.ocr_ms:.0f} • correction {timing.correction_ms:.1f} ms"
            )
        )
        if self.config.get().get("ocr", {}).get("debug_logging"):
            self._write_performance_debug(measured)

    def _speech_finished(self, request_id: int, text: str) -> None:
        self.text_state.end_speech(text, request_id)

    def _apply_initial_hotkeys(self) -> None:
        settings = self.config.get()
        try:
            self.apply_hotkeys(
                settings["hotkeys"]["fixed"],
                settings["hotkeys"]["snippet"],
                settings["hotkeys"].get("read_again", ""),
            )
        except Exception as exc:
            self.ui.set_hotkey_status(False)
            self.ui.set_status(f"Hotkeys are inactive: {exc}", error=True)

    def apply_hotkeys(self, fixed: str, snippet: str, read_again: str = "") -> None:
        """Register the requested global keys and persist them only after success."""
        fixed_key, snippet_key, again_key = self.hotkeys.apply_all(fixed, snippet, read_again)
        self.config.update(hotkeys={"fixed": fixed_key, "snippet": snippet_key, "read_again": again_key})
        self.ui.set_hotkeys(fixed_key, snippet_key, again_key)
        self.ui.set_hotkey_status(bool(fixed_key or snippet_key or again_key), fixed_key, snippet_key, again_key)
        if not fixed_key and not snippet_key and not again_key:
            self.ui.set_status("Global shortcuts are disabled. You can still use the buttons and tray menu.")

    def set_shortcuts_paused(self, paused: bool) -> None:
        """Prevent an existing shortcut from firing while that same chord is recorded."""
        if paused:
            self.hotkeys.stop()
            return
        settings = self.config.get()["hotkeys"]
        try:
            self.hotkeys.apply_all(settings["fixed"], settings["snippet"], settings.get("read_again", ""))
        except Exception as exc:
            self.ui.set_hotkey_status(False)
            self.ui.set_status(f"Shortcuts could not resume after recording: {exc}", error=True)

    def show_window(self) -> None:
        """Restore settings from the tray and bring them in front of other windows."""
        if self._closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        """Explicitly remove settings from the taskbar through the tray action."""
        if not self._closed:
            self.root.withdraw()

    def minimize_window(self) -> None:
        """Move settings out of a capture without removing it from the taskbar."""
        if not self._closed:
            if self.root.state() == "withdrawn":
                # Respect an explicit Hide-to-tray choice across later hotkeys.
                return
            self.root.iconify()
            self.root.update_idletasks()
            flush_windows_compositor()

    def _refresh_profiles_ui(self, status: str = "") -> None:
        settings = self.config.get()
        resolution = self.profiles.resolve_selected()
        self.ui.set_profiles(
            settings["capture_profiles"],
            settings["selected_profile_id"],
            resolution.box if resolution.available else None,
            "" if resolution.available else resolution.reason,
        )
        if status:
            self.ui.set_status(status)

    def create_capture_profile(self, name: str) -> None:
        profile = self.profiles.create(name)
        self._refresh_profiles_ui(f'Profile "{profile["name"]}" created. Set its capture area next.')

    def rename_capture_profile(self, profile_id: str, name: str) -> None:
        profile = self.profiles.rename(profile_id, name)
        self._refresh_profiles_ui(f'Profile renamed to "{profile["name"]}".')

    def delete_capture_profile(self, profile_id: str) -> None:
        self.profiles.delete(profile_id)
        self._refresh_profiles_ui("Capture profile deleted.")

    def select_capture_profile(self, profile_id: str) -> None:
        profile = self.profiles.select(profile_id)
        resolution = self.profiles.resolve_selected()
        self._refresh_profiles_ui()
        if resolution.available:
            self.ui.set_status(f'Using capture profile "{profile["name"]}".')
        else:
            self.ui.set_status(resolution.reason, error=True)

    def open_box_editor(self) -> None:
        """Hide settings while the user edits a reusable fixed subtitle box."""
        if self._overlay is not None:
            return
        was_visible = self.root.state() not in {"withdrawn", "iconic"}
        resolution = self.profiles.resolve_selected() if hasattr(self, "profiles") else None
        existing_box = resolution.box if resolution is not None and resolution.available else self.config.get().get("fixed_box")
        self.minimize_window()

        def confirmed(box: list[int]) -> None:
            self._overlay = None
            if hasattr(self, "profiles"):
                self.profiles.update_selected_area(box)
                self._refresh_profiles_ui("Capture area saved to the selected profile.")
            else:
                self.config.update(fixed_box=box)
                self.ui.set_box(box)
                self.ui.set_status("Fixed read box saved.")
            if was_visible:
                self.show_window()

        def cancelled() -> None:
            self._overlay = None
            self.ui.set_status("Fixed read box unchanged.")
            if was_visible:
                self.show_window()

        self._overlay = BoxEditorOverlay(self.root, existing_box, confirmed, cancelled)

    def open_quick_snippet(self, restore_settings_after: bool = False) -> None:
        """Open a temporary Snipping Tool-style selector without modifying fixed-box state."""
        if self._overlay is not None:
            return
        was_visible = self.root.state() not in {"withdrawn", "iconic"}
        self.minimize_window()

        def captured(box: list[int]) -> None:
            self._overlay = None
            self.capture_box(box, "Quick snippet")

        def cancelled() -> None:
            self._overlay = None
            if restore_settings_after and was_visible:
                self.show_window()

        self._overlay = QuickSnippetOverlay(self.root, captured, cancelled)

    def read_fixed_box(self, hide_settings: bool = False, requested_at: float | None = None) -> None:
        """Capture and read the saved subtitle rectangle selected by the fixed hotkey."""
        if self._overlay is not None:
            return
        if hasattr(self, "profiles"):
            resolution = self.profiles.resolve_selected()
            box = resolution.box
            unavailable_reason = resolution.reason
        else:
            box = self.config.get().get("fixed_box")
            unavailable_reason = "No fixed box is saved. Choose Draw / Edit Read Box first."
        if not box:
            self.ui.set_status(unavailable_reason, error=True)
            return
        if hide_settings:
            self.minimize_window()
        self.capture_box(box, "Fixed box", requested_at=requested_at)

    def stop_speech(self) -> None:
        """Interrupt current playback and clear speech waiting in the queue."""
        self.tts.stop()
        if hasattr(self, "text_state"):
            self.text_state.end_speech()
        if hasattr(self, "_timing_lock"):
            with self._timing_lock:
                self._pending_speech_timing = None
        self.ui.set_status("Speech stopped and the queue was cleared.")

    def read_again(self) -> None:
        """Replay the last corrected OCR text without another capture or correction."""
        text = self.text_state.last_successful_text.strip()
        if not text:
            self.ui.set_status("There is no successfully captured text to read again.", error=True)
            return
        settings = self.config.get()
        self.tts.stop()
        self.text_state.end_speech()
        if hasattr(self, "_timing_lock"):
            with self._timing_lock:
                self._pending_speech_timing = None
        replace = getattr(self.tts, "replace", None) or self.tts.speak
        replace(text, settings["voice"], settings["rate"], settings["volume"])
        self.ui.set_status("Reading the last captured text again.")

    def clear_text_history(self) -> None:
        self.text_state.clear_history()

    def process_ocr_text(self, raw_text: str, settings: dict[str, object]) -> CorrectionResult:
        """Apply the configured post-processing while retaining the OCR source text."""
        return self.corrector.correct(raw_text, CorrectionOptions.from_mapping(settings))

    def preload_correction_engine(self) -> None:
        """Avoid a first-read delay when dictionary correction is selected."""
        strength = self.config.get().get("ocr", {}).get("strength")
        if strength in {"balanced", "strong"}:
            threading.Thread(target=self.corrector.warm_up, name="ocr-dictionary-load", daemon=True).start()

    def _write_correction_debug(self, result: CorrectionResult) -> None:
        """Write an opt-in, size-limited correction trace outside normal UI status."""
        log_path = self.config.path.with_name("ocr_debug.log")
        logger = logging.getLogger(f"game_text_reader.ocr.{log_path}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
        changes = "; ".join(f"{item.original!r} -> {item.replacement!r} ({item.reason})" for item in result.corrections) or "none"
        logger.info("Raw OCR:\n%s\nCorrected:\n%s\nCorrections: %s", result.raw_text, result.corrected_text, changes)

    def _write_performance_debug(self, timings: dict[str, float]) -> None:
        log_path = self.config.path.with_name("ocr_debug.log")
        logger = logging.getLogger(f"game_text_reader.ocr.{log_path}")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
        logger.info(
            "Pipeline timing: dispatch %.1f ms; capture %.1f ms; OCR %.1f ms; correction %.1f ms; speech started %.1f ms after request; handoff %.1f ms after correction",
            timings["dispatch_ms"],
            timings["capture_ms"],
            timings["ocr_ms"],
            timings["correction_ms"],
            timings["speech_start_ms"],
            timings["speech_handoff_ms"],
        )

    @staticmethod
    def _grab_screen(box: list[int]):
        try:
            return ImageGrab.grab(bbox=tuple(box), all_screens=True)
        except TypeError:
            return ImageGrab.grab(bbox=tuple(box))

    def capture_box(
        self,
        box: list[int],
        source: str,
        restore_after_grab: Callable[[], None] | None = None,
        requested_at: float | None = None,
    ) -> None:
        """Submit a capture while speech continues on its independent worker."""
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            self.ui.set_status("The selected region is invalid.", error=True)
            if restore_after_grab:
                restore_after_grab()
            return
        settings = self.config.get()
        self.ui.set_status(f"{source}: capturing and reading…")
        after_capture = (lambda: self._schedule(restore_after_grab)) if restore_after_grab else None
        try:
            self.capture_worker.submit(
                box,
                source,
                settings,
                requested_at=requested_at,
                on_capture_complete=after_capture,
            )
        except Exception as exc:
            if restore_after_grab:
                restore_after_grab()
            self.ui.set_status(f"{source} capture could not start: {exc}", error=True)

    def _capture_succeeded(self, job: CaptureJob, result: CorrectionResult, timings: PipelineTimings) -> None:
        settings = dict(job.settings)
        if settings.get("ocr", {}).get("debug_logging"):
            self._write_correction_debug(result)
        self.text_state.accept_success(result)

        def publish_result() -> None:
            self.ui.set_last_result(result)
            self.ui.set_read_again_enabled(self.text_state.can_read_again)

        self._schedule(publish_result)
        final_text = result.corrected_text.strip()
        if not final_text:
            self._schedule(lambda: self.ui.set_status(f"{job.source}: no readable text found."))
            return
        with self._timing_lock:
            self._pending_speech_timing = (final_text, timings)
        change_count = len(result.corrections)
        suffix = f" ({change_count} correction{'s' if change_count != 1 else ''})" if change_count else ""
        voice = str(settings.get("voice", ""))
        rate = int(settings.get("rate", 0))
        volume = int(settings.get("volume", 100))
        speech_settings = settings.get("speech", {})
        capture_mode = speech_settings.get("capture_mode", "replace")
        max_overlap = speech_settings.get("max_overlap", 2)
        if capture_mode == "replace":
            speech_status = "replacing current speech"
        elif capture_mode == "overlap":
            speech_status = f"overlapping speech (up to {max_overlap} voices)"
        else:
            speech_status = "queued as the next line"
        self._schedule(lambda: self.ui.set_status(f"{job.source}: {speech_status}{suffix}."))
        if capture_mode == "replace":
            replace = getattr(self.tts, "replace", None) or self.tts.speak
            replace(final_text, voice, rate, volume)
        elif capture_mode == "overlap":
            overlap = getattr(self.tts, "overlap", None) or self.tts.speak
            overlap(final_text, voice, rate, volume)
        else:
            enqueue = getattr(self.tts, "enqueue", None) or self.tts.speak
            enqueue(final_text, voice, rate, volume)

    def _capture_failed(self, job: CaptureJob, exc: Exception) -> None:
        message = str(exc) if isinstance(exc, OcrError) else f"{job.source} capture failed: {exc}"
        self._schedule(lambda: self.ui.set_status(message, error=True))

    def quit_app(self) -> None:
        """Release external hooks and Windows resources before closing the process."""
        if self._closed:
            return
        self._closed = True
        if self._overlay is not None:
            self._overlay.close()
            self._overlay = None
        self.hotkeys.stop()
        self.tray.stop()
        self.capture_worker.close()
        self.tts.shutdown()
        try:
            self.ui.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        """Enter Tk's event loop until the tray's Quit command is chosen."""
        try:
            self.root.mainloop()
        finally:
            self.quit_app()


def main() -> None:
    """Create and run the desktop application."""
    set_windows_app_identity()
    enable_dpi_awareness()
    GameTextReaderApplication().run()


if __name__ == "__main__":
    main()
