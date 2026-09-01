"""Application entry point for Minimal Windows Game Text Reader."""

from __future__ import annotations

import ctypes
import logging
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler
from queue import Empty, SimpleQueue
from typing import Callable

from PIL import ImageGrab

from config import ConfigStore
from hotkey_manager import HotkeyManager
from ocr_correction import CorrectionOptions, CorrectionResult, OcrCorrector
from ocr_engine import OcrEngine, OcrError
from overlay import BoxEditorOverlay, QuickSnippetOverlay
from settings_ui import SettingsUI
from tray_app import TrayApp
from tts_engine import TtsEngine


def enable_dpi_awareness() -> None:
    """Ask Windows for per-monitor DPI-aware physical coordinates before creating Tk."""
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE is required for screenshot pixels,
        # overlay coordinates, and game window coordinates to match at >100% DPI.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class GameTextReaderApplication:
    """Coordinate GUI, tray, hotkeys, native OCR, and queued speech synthesis."""

    def __init__(self) -> None:
        self.config = ConfigStore()
        self.config.load()
        self.root = tk.Tk()
        self._closed = False
        self._scheduled_actions: SimpleQueue[Callable[[], None]] = SimpleQueue()
        self._capture_lock = threading.Lock()
        self._overlay: BoxEditorOverlay | QuickSnippetOverlay | None = None
        self.ocr = OcrEngine()
        self.corrector = OcrCorrector()
        self.tts = TtsEngine(on_error=self._speech_error)
        self.hotkeys = HotkeyManager(
            on_fixed=lambda: self._schedule(lambda: self.read_fixed_box(hide_settings=True)),
            on_snippet=lambda: self._schedule(self.open_quick_snippet),
        )
        self.ui = SettingsUI(
            self.root,
            self.config,
            self.tts,
            on_draw_box=self.open_box_editor,
            on_read_box=self.read_fixed_box,
            on_quick_snippet=lambda: self.open_quick_snippet(restore_settings_after=True),
            on_apply_hotkeys=self.apply_hotkeys,
            on_ocr_settings_changed=self.preload_correction_engine,
            on_shortcut_recording=self.set_shortcuts_paused,
        )
        self.tray = TrayApp(
            on_show=lambda: self._schedule(self.show_window),
            on_read_fixed=lambda: self._schedule(self.read_fixed_box),
            on_quick_snippet=lambda: self._schedule(self.open_quick_snippet),
            on_stop_speech=lambda: self._schedule(self.stop_speech),
            on_quit=lambda: self._schedule(self.quit_app),
        )
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._apply_initial_hotkeys()
        self.tray.start()
        self.root.after(20, self._drain_scheduled_actions)
        self.preload_correction_engine()

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
        self._schedule(lambda: self.ui.set_status(f"Speech error: {message}", error=True))

    def _apply_initial_hotkeys(self) -> None:
        settings = self.config.get()
        try:
            self.apply_hotkeys(settings["hotkeys"]["fixed"], settings["hotkeys"]["snippet"])
        except Exception as exc:
            self.ui.set_hotkey_status(False)
            self.ui.set_status(f"Hotkeys are inactive: {exc}", error=True)

    def apply_hotkeys(self, fixed: str, snippet: str) -> None:
        """Register the requested global keys and persist them only after success."""
        fixed_key, snippet_key = self.hotkeys.apply(fixed, snippet)
        self.config.update(hotkeys={"fixed": fixed_key, "snippet": snippet_key})
        self.ui.set_hotkeys(fixed_key, snippet_key)
        self.ui.set_hotkey_status(bool(fixed_key or snippet_key), fixed_key, snippet_key)
        if not fixed_key and not snippet_key:
            self.ui.set_status("Global shortcuts are disabled. You can still use the buttons and tray menu.")

    def set_shortcuts_paused(self, paused: bool) -> None:
        """Prevent an existing shortcut from firing while that same chord is recorded."""
        if paused:
            self.hotkeys.stop()
            return
        settings = self.config.get()["hotkeys"]
        try:
            self.hotkeys.apply(settings["fixed"], settings["snippet"])
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
        """Keep hotkeys and the tray app running after the settings window is closed."""
        if not self._closed:
            self.root.withdraw()

    def open_box_editor(self) -> None:
        """Hide settings while the user edits a reusable fixed subtitle box."""
        if self._overlay is not None:
            return
        was_visible = self.root.state() != "withdrawn"
        self.root.withdraw()

        def confirmed(box: list[int]) -> None:
            self._overlay = None
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

        self._overlay = BoxEditorOverlay(self.root, self.config.get().get("fixed_box"), confirmed, cancelled)

    def open_quick_snippet(self, restore_settings_after: bool = False) -> None:
        """Open a temporary Snipping Tool-style selector without modifying fixed-box state."""
        if self._overlay is not None:
            return
        was_visible = self.root.state() != "withdrawn"
        self.root.withdraw()

        def captured(box: list[int]) -> None:
            self._overlay = None
            restore = self.show_window if restore_settings_after and was_visible else None
            self.capture_box(box, "Quick snippet", restore_after_grab=restore)

        def cancelled() -> None:
            self._overlay = None
            if restore_settings_after and was_visible:
                self.show_window()

        self._overlay = QuickSnippetOverlay(self.root, captured, cancelled)

    def read_fixed_box(self, hide_settings: bool = False) -> None:
        """Capture and read the saved subtitle rectangle selected by the fixed hotkey."""
        if self._overlay is not None:
            return
        if hide_settings:
            self.hide_window()
        box = self.config.get().get("fixed_box")
        if not box:
            self.ui.set_status("No fixed box is saved. Choose Draw / Edit Read Box first.", error=True)
            return
        self.capture_box(box, "Fixed box")

    def stop_speech(self) -> None:
        """Interrupt current playback and clear speech waiting in the queue."""
        self.tts.stop()
        self.ui.set_status("Speech stopped and the queue was cleared.")

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

    def capture_box(self, box: list[int], source: str, restore_after_grab: Callable[[], None] | None = None) -> None:
        """Screenshot, OCR, and queue speech on a worker thread without freezing play."""
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            self.ui.set_status("The selected region is invalid.", error=True)
            if restore_after_grab:
                restore_after_grab()
            return
        if not self._capture_lock.acquire(blocking=False):
            self.ui.set_status("A screen read is already in progress. Try again in a moment.")
            if restore_after_grab:
                restore_after_grab()
            return
        settings = self.config.get()
        self.ui.set_status(f"{source}: capturing and reading…")

        def worker() -> None:
            restored = False
            try:
                try:
                    image = ImageGrab.grab(bbox=tuple(box), all_screens=True)
                except TypeError:
                    image = ImageGrab.grab(bbox=tuple(box))
                if restore_after_grab:
                    self._schedule(restore_after_grab)
                    restored = True
                raw_text = self.ocr.recognise(image)
                result = self.process_ocr_text(raw_text, settings.get("ocr", {}))
                if settings.get("ocr", {}).get("debug_logging"):
                    self._write_correction_debug(result)
                self._schedule(lambda: self.ui.set_last_result(result))
                if result.corrected_text:
                    # The newest screen read is the useful one during play.
                    # Interrupt stale speech instead of building a long queue.
                    self.tts.stop()
                    self.tts.speak(result.corrected_text, settings["voice"], settings["rate"], settings["volume"])
                    change_count = len(result.corrections)
                    suffix = f" ({change_count} correction{'s' if change_count != 1 else ''})" if change_count else ""
                    self._schedule(lambda: self.ui.set_status(f"{source}: text sent to speech{suffix}."))
                else:
                    self._schedule(lambda: self.ui.set_status(f"{source}: no readable text found."))
            except OcrError as exc:
                self._schedule(lambda: self.ui.set_status(str(exc), error=True))
            except Exception as exc:
                self._schedule(lambda: self.ui.set_status(f"{source} capture failed: {exc}", error=True))
            finally:
                self._capture_lock.release()
                if restore_after_grab and not restored:
                    self._schedule(restore_after_grab)

        threading.Thread(target=worker, name="screen-ocr", daemon=True).start()

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
        self.tts.shutdown()
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
    enable_dpi_awareness()
    GameTextReaderApplication().run()


if __name__ == "__main__":
    main()
