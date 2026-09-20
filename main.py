"""Application entry point for Minimal Windows Game Text Reader."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Callable

from PIL import ImageGrab

from auto_read import AutoReadWatcher
from app_resources import apply_window_icon
from appearance import flush_windows_compositor, resolve_theme
from capture_pipeline import CaptureJob, CaptureWorker, PipelineTimings
from capture_profiles import CaptureProfileManager
from config import ConfigStore
from hotkey_manager import HotkeyManager
from ocr_correction import CorrectionOptions, CorrectionResult, OcrCorrector
from ocr_engine import OcrEngine, OcrError
from overlay import BoxEditorOverlay, QuickSnippetOverlay
from reader_state import ReaderTextState
from settings_ui import SettingsUI
from speech_text import prepare_for_speech
from startup_registration import (
    STARTUP_ARGUMENT,
    StartupRegistration,
    StartupRegistrationError,
)
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

    def __init__(
        self,
        start_hidden: bool = False,
        startup_registration: StartupRegistration | None = None,
    ) -> None:
        self.config = ConfigStore()
        settings = self.config.load()
        self.startup_registration = startup_registration or StartupRegistration()
        startup_error = self._reconcile_startup_registration(settings)
        settings = self.config.get()
        self.root = tk.Tk()
        if start_hidden:
            self.root.withdraw()
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
            on_document_started_with_id=self._speech_document_started,
            on_word_with_id=self._speech_word,
            initial_voice_id=settings["voice"],
            initial_capture_mode=settings.get("speech", {}).get("capture_mode", "replace"),
            initial_max_overlap=settings.get("speech", {}).get("max_overlap", 2),
        )
        self.capture_worker = CaptureWorker(
            capture=self._grab_screen,
            recognise=lambda image, snapshot, is_current: self.ocr.recognise_with_details(
                image, mode=snapshot.get("ocr", {}).get("recognition_mode", "standard"),
                is_current=is_current,
            ),
            correct=lambda raw, snapshot: self.process_ocr_text(raw, snapshot.get("ocr", {})),
            on_result=self._capture_succeeded,
            on_error=self._capture_failed,
            on_start=self.ocr.warm_up,
            on_close=self.ocr.close,
        )
        self._capture_submit_lock = threading.Lock()
        self._manual_capture_pending = False
        self.auto_read = AutoReadWatcher(
            self._grab_screen, self._submit_auto_image, self._auto_read_state,
            area_valid=self._auto_area_valid,
        )
        self.hotkeys = HotkeyManager(
            on_fixed=self._fixed_hotkey_received,
            on_snippet=lambda: self._schedule(self.open_quick_snippet),
            on_read_again=lambda: self._schedule(self.read_again),
            on_auto_read=lambda: self._schedule(self.toggle_auto_read),
            on_error=lambda message: self._schedule(lambda: self.ui.set_status(message, error=True)),
            on_event=self._hotkey_debug,
        )
        self.ui = SettingsUI(
            self.root,
            self.config,
            self.tts,
            on_draw_box=self.open_box_editor,
            on_read_box=lambda: self.read_fixed_box(hide_settings=True),
            on_toggle_auto_read=self.toggle_auto_read,
            on_stop_speech=self.stop_speech,
            on_quick_snippet=lambda: self.open_quick_snippet(restore_settings_after=True),
            on_apply_hotkeys=self.apply_hotkeys,
            on_ocr_settings_changed=self.preload_correction_engine,
            on_shortcut_recording=self.set_shortcuts_paused,
            on_read_again=self.read_again,
            on_clear_text=self.clear_text_history,
            on_manual_text_changed=self.accept_manual_text,
            on_startup_changed=self.set_launch_at_startup,
            on_import_settings=self.import_older_settings,
            on_seek=self.seek_reader,
            on_review_result=self.accept_reviewed_result,
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
            on_toggle_auto_read=lambda: self._schedule(self.toggle_auto_read),
        )
        self.root.protocol("WM_DELETE_WINDOW", self.minimize_window)
        self._apply_initial_hotkeys()
        tray_started = self.tray.start()
        self.root.after(20, self._drain_scheduled_actions)
        self.preload_correction_engine()
        self._refresh_profiles_ui()
        self._apply_startup_visibility(start_hidden, tray_started, startup_error)
        if self.config.load_warning:
            self.ui.set_status(self.config.load_warning, error=True)

    def _apply_startup_visibility(
        self, start_hidden: bool, tray_started: bool, startup_error: str = ""
    ) -> None:
        """Keep sign-in launches hidden only when the tray can restore them."""

        if startup_error:
            self.ui.set_status(startup_error, error=True)
        if start_hidden and tray_started:
            # Run after the placement controller restores a saved maximized state.
            self.root.after_idle(self.hide_window)
        elif start_hidden:
            self.root.deiconify()
            self.ui.set_status(
                "The tray icon could not start, so settings were opened instead.",
                error=True,
            )

    def _reconcile_startup_registration(self, settings: dict[str, object]) -> str:
        """Restore a configured Run entry and repair a moved executable path."""

        configured = bool(settings.get("startup", {}).get("enabled", False))
        try:
            registered = self.startup_registration.is_enabled()
            if configured:
                self.startup_registration.set_enabled(True)
            elif registered:
                self.config.update(startup={"enabled": True})
        except StartupRegistrationError as exc:
            self.config.update(startup={"enabled": False})
            return str(exc)
        return ""

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

        def report() -> None:
            self.ui.clear_speech_progress()
            self.ui.set_status(f"Speech error: {message}", error=True)

        self._schedule(report)

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
        self._schedule(lambda: self.ui.clear_speech_progress(request_id))

    def _speech_document_started(self, request_id: int, source_text: str) -> None:
        """Give the newest mapped reading highlight ownership immediately."""

        self._schedule(
            lambda: self.ui.begin_speech_progress(request_id, source_text)
        )

    def _speech_word(
        self, request_id: int, source_text: str, source_start: int, source_end: int
    ) -> None:
        """Hand native word timing back to Tk without touching it off-thread."""

        self._schedule(
            lambda: self.ui.show_speech_progress(
                request_id, source_text, source_start, source_end
            )
        )

    def _apply_initial_hotkeys(self) -> None:
        settings = self.config.get()
        try:
            self.apply_hotkeys(
                settings["hotkeys"]["fixed"],
                settings["hotkeys"]["snippet"],
                settings["hotkeys"].get("read_again", ""),
                settings["hotkeys"].get("auto_read", ""),
            )
        except Exception as exc:
            self.ui.set_hotkey_status(False)
            self.ui.set_status(f"Hotkeys are inactive: {exc}", error=True)

    def apply_hotkeys(self, fixed: str, snippet: str, read_again: str = "", auto_read: str = "") -> None:
        """Register the requested global keys and persist them only after success."""
        previous = self.config.get()['hotkeys']
        try:
            registered = self.hotkeys.apply_all(fixed, snippet, read_again, auto_read)
            # Legacy callers/tests can construct the three-action manager.
            fixed_key, snippet_key, again_key, auto_key = (*registered, "")[:4]
            try:
                self.config.update(hotkeys={"fixed": fixed_key, "snippet": snippet_key, "read_again": again_key, "auto_read": auto_key})
            except Exception:
                self.hotkeys.apply_all(previous['fixed'], previous['snippet'], previous.get('read_again', ''), previous.get('auto_read', ''))
                raise
        except Exception:
            keys = self.hotkeys.active_keys
            self.ui.set_hotkey_status(self.hotkeys.is_running, *keys)
            raise
        self.ui.set_hotkeys(fixed_key, snippet_key, again_key, auto_key)
        self.ui.set_hotkey_status(bool(fixed_key or snippet_key or again_key or auto_key), fixed_key, snippet_key, again_key, auto_key)
        if not fixed_key and not snippet_key and not again_key and not auto_key:
            self.ui.set_status("Global shortcuts are disabled. You can still use the buttons and tray menu.")

    def _hotkey_debug(self, message: str) -> None:
        if not self.config.get().get('ocr', {}).get('debug_logging'):
            return
        path = self.config.path.with_name('ocr_debug.log')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(f'game_text_reader.ocr.{path}')
            if not logger.handlers:
                handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
                logger.propagate = False
            logger.info('Shortcut: %s', message)
        except OSError:
            pass

    def set_shortcuts_paused(self, paused: bool) -> None:
        """Prevent an existing shortcut from firing while that same chord is recorded."""
        if paused:
            self.hotkeys.stop()
            return
        settings = self.config.get()["hotkeys"]
        try:
            self.hotkeys.apply_all(settings["fixed"], settings["snippet"], settings.get("read_again", ""), settings.get("auto_read", ""))
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
        self.auto_read.stop("Auto-Read stopped because the capture profile changed.")
        profile = self.profiles.create(name)
        self._refresh_profiles_ui(f'Profile "{profile["name"]}" created. Set its capture area next.')

    def rename_capture_profile(self, profile_id: str, name: str) -> None:
        self.auto_read.stop("Auto-Read stopped because the capture profile changed.")
        profile = self.profiles.rename(profile_id, name)
        self._refresh_profiles_ui(f'Profile renamed to "{profile["name"]}".')

    def delete_capture_profile(self, profile_id: str) -> None:
        self.auto_read.stop("Auto-Read stopped because the capture profile changed.")
        self.profiles.delete(profile_id)
        self._refresh_profiles_ui("Capture profile deleted.")

    def select_capture_profile(self, profile_id: str) -> None:
        self.auto_read.stop("Auto-Read stopped because the capture profile changed.")
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
        self.auto_read.stop("Auto-Read stopped while the capture area is being edited.")
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

        self._overlay = BoxEditorOverlay(
            self.root, existing_box, confirmed, cancelled,
            palette=resolve_theme(self.config.get()["theme"]),
        )

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

        try:
            self._overlay = QuickSnippetOverlay(self.root, captured, cancelled)
            self._hotkey_debug('Snippet overlay opened')
        except Exception as exc:
            self._overlay = None
            self.ui.set_status(f'Could not open snippet selector: {exc}', error=True)
            self._hotkey_debug(f'Snippet overlay failed: {exc}')

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

    def _auto_read_state(self, active: bool, message: str, error: bool) -> None:
        """Marshal watcher status to Tk; its polling thread never touches widgets."""
        def publish() -> None:
            self.ui.set_auto_read_state(active, message)
            self.ui.set_status(message, error=error)
        self._schedule(publish)

    def _auto_area_valid(self, box: tuple[int, int, int, int]) -> bool:
        resolution = self.profiles.resolve_selected()
        return resolution.available and resolution.box == list(box)

    def toggle_auto_read(self) -> None:
        """Arm the selected fixed box or stop the current foreground watch."""
        if self.auto_read.active:
            self.auto_read.stop()
            return
        resolution = self.profiles.resolve_selected()
        if not resolution.available or not resolution.box:
            self.ui.set_status(resolution.reason, error=True)
            return
        settings = self.config.get()
        profile_id = settings.get("selected_profile_id")
        profile = next(
            (str(item.get("name", "Profile")) for item in settings["capture_profiles"]
             if item.get("id") == profile_id), "Profile"
        )
        self.auto_read.start(resolution.box, profile)

    def _submit_auto_image(
        self, image: object, box: tuple[int, int, int, int], session: int, revision: int
    ) -> bool:
        """Manual captures outrank watcher scans and share the newest-job worker."""
        with self._capture_submit_lock:
            if self._manual_capture_pending or not self.auto_read.is_current(session, revision):
                return False
            if not self.capture_worker.wait_until_idle(0):
                return False
            self.capture_worker.submit(
                box, "Auto-Read", self.config.get(), image=image,
                auto_session=session, auto_revision=revision,
            )
            return True

    def stop_speech(self) -> None:
        """Interrupt current playback and clear speech waiting in the queue."""
        self.auto_read.stop("Auto-Read stopped with audio.")
        self.tts.stop()
        if hasattr(self, "text_state"):
            self.text_state.end_speech()
        if hasattr(self, "_timing_lock"):
            with self._timing_lock:
                self._pending_speech_timing = None
        self.ui.clear_speech_progress()
        self.ui.set_status("Speech stopped and the queue was cleared.")

    def read_again(self) -> None:
        """Replay the last corrected OCR text without another capture or correction."""
        display_text = self.text_state.last_successful_text
        document = prepare_for_speech(display_text)
        if not document.spoken_text:
            self.ui.set_status("There is no successfully captured text to read again.", error=True)
            return
        settings = self.config.get()
        self.tts.stop()
        self.text_state.end_speech()
        if hasattr(self, "_timing_lock"):
            with self._timing_lock:
                self._pending_speech_timing = None
        replace = getattr(self.tts, "replace", None) or self.tts.speak
        replace(document, settings["voice"], settings["rate"], settings["volume"])
        self.ui.set_status("Reading the last captured text again.")

    def seek_reader(self, request_id: int, source_text: str, source_offset: int) -> None:
        if source_text != self.text_state.last_successful_text:
            return
        ticket = self.tts.seek_to_source(request_id, source_text, source_offset)
        if ticket is not None:
            self.ui.set_status('Continuing from the selected word.')

    def accept_reviewed_result(self, result: CorrectionResult) -> None:
        self.tts.stop()
        self.text_state.accept_success(result)

    def accept_manual_text(self, text: str) -> None:
        """Make the user's editor contents the authoritative Read Again text."""

        self.text_state.accept_manual_text(text)

    def set_launch_at_startup(self, enabled: bool) -> None:
        """Apply Windows startup registration before persisting the preference."""

        previous = bool(self.config.get().get("startup", {}).get("enabled", False))
        self.startup_registration.set_enabled(enabled)
        try:
            self.config.update(startup={"enabled": bool(enabled)})
        except Exception:
            try:
                self.startup_registration.set_enabled(previous)
            except StartupRegistrationError:
                pass
            raise

    def import_older_settings(self, folder: Path) -> None:
        """Validate and store settings selected from an older portable copy."""

        self.config.import_from_older_app_folder(folder)

    def clear_text_history(self) -> None:
        self.text_state.clear_history()

    def process_ocr_text(self, raw_text: str, settings: dict[str, object]) -> CorrectionResult:
        """Apply the configured post-processing while retaining the OCR source text."""
        return self.corrector.correct(raw_text, CorrectionOptions.from_mapping(settings))

    def preload_correction_engine(self) -> None:
        """Avoid a first-read delay when dictionary correction is selected."""
        enabled = self.config.get().get("ocr", {}).get("enabled", True)
        if enabled:
            threading.Thread(target=self.corrector.warm_up, name="ocr-dictionary-load", daemon=True).start()

    def _write_correction_debug(self, result: CorrectionResult, job: CaptureJob, timings: PipelineTimings) -> None:
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
        suggestions = "; ".join(f"{item.original!r} -> {item.alternatives!r} ({item.reason})"
                                for item in result.suggestions) or "none"
        logger.info("Raw OCR:\n%s\nCorrected:\n%s\nCorrections: %s\nNeeds review: %s",
                    result.raw_text, result.corrected_text, changes, suggestions)
        logger.info("Text extraction: mode=%s variant=%s passes=%d OCR=%.1f ms",
                    job.settings.get("ocr", {}).get("recognition_mode", "standard"),
                    timings.ocr_variant, timings.ocr_attempts, timings.ocr_ms)

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
            with self._capture_submit_lock:
                self._manual_capture_pending = True
                self.auto_read.manual_started()
                self.capture_worker.submit(
                    box, source, settings, requested_at=requested_at,
                    on_capture_complete=after_capture,
                )
        except Exception as exc:
            self._manual_capture_pending = False
            if restore_after_grab:
                restore_after_grab()
            self.ui.set_status(f"{source} capture could not start: {exc}", error=True)

    def _capture_succeeded(self, job: CaptureJob, result: CorrectionResult, timings: PipelineTimings) -> None:
        is_auto = job.auto_session is not None
        if is_auto:
            if not self.auto_read.accept_result(job.auto_session, job.auto_revision, result.corrected_text):
                return
        else:
            self._manual_capture_pending = False
            if hasattr(self, "auto_read"):
                self.auto_read.remember_manual(result.corrected_text)
        settings = dict(job.settings)
        if settings.get("ocr", {}).get("debug_logging"):
            self._write_correction_debug(result, job, timings)
        self.text_state.accept_success(result)

        def publish_result() -> None:
            self.ui.set_last_result(result)
            self.ui.set_read_again_enabled(self.text_state.can_read_again)

        self._schedule(publish_result)
        final_text = result.corrected_text.strip()
        document = prepare_for_speech(final_text)
        spoken_text = document.spoken_text
        if not spoken_text:
            self._schedule(lambda: self.ui.set_status(f"{job.source}: no readable text found."))
            return
        with self._timing_lock:
            self._pending_speech_timing = (spoken_text, timings)
        change_count = len(result.corrections)
        suffix = f" ({change_count} correction{'s' if change_count != 1 else ''})" if change_count else ""
        voice = str(settings.get("voice", ""))
        rate = int(settings.get("rate", 0))
        volume = int(settings.get("volume", 100))
        speech_settings = settings.get("speech", {})
        capture_mode = "replace" if is_auto else speech_settings.get("capture_mode", "replace")
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
            replace(document, voice, rate, volume)
        elif capture_mode == "overlap":
            overlap = getattr(self.tts, "overlap", None) or self.tts.speak
            overlap(document, voice, rate, volume)
        else:
            enqueue = getattr(self.tts, "enqueue", None) or self.tts.speak
            enqueue(document, voice, rate, volume)

    def _capture_failed(self, job: CaptureJob, exc: Exception) -> None:
        if job.auto_session is not None:
            self.auto_read.note_error(job.auto_session, job.auto_revision, str(exc))
            return
        self._manual_capture_pending = False
        message = str(exc) if isinstance(exc, OcrError) else f"{job.source} capture failed: {exc}"
        self._schedule(lambda: self.ui.set_status(message, error=True))

    def quit_app(self) -> None:
        """Release external hooks and Windows resources before closing the process."""
        if self._closed:
            return
        self._closed = True
        self.auto_read.stop(notify=False)
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


def main(argv: list[str] | None = None) -> None:
    """Create and run the desktop application."""
    arguments = sys.argv[1:] if argv is None else argv
    set_windows_app_identity()
    enable_dpi_awareness()
    GameTextReaderApplication(start_hidden=STARTUP_ARGUMENT in arguments).run()


if __name__ == "__main__":
    main()
