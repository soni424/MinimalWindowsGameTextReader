"""Automated smoke tests for the native Windows Game Text Reader components.

Run with ``python test_app.py`` on Windows.  The speech test deliberately plays
a short phrase through the configured default Windows speaker.
"""

from __future__ import annotations

import threading
import unittest
import ctypes
from pathlib import Path
from queue import SimpleQueue
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from appearance import apply_windows_title_bar
from config import ConfigStore, validate_config
from hotkey_manager import HotkeyError, HotkeyManager, normalise_hotkey, to_pynput_hotkey
from main import GameTextReaderApplication
from ocr_correction import OcrCorrector
from ocr_engine import OcrEngine, OcrError
from settings_ui import SettingsUI, _ShortcutRecorderDialog
from tts_engine import TtsEngine


@unittest.skipUnless(__import__("os").name == "nt", "Windows native APIs are required")
class WindowsComponentTests(unittest.TestCase):
    """Exercise the four native integration points without launching the GUI."""

    def test_fixed_hotkey_uses_the_saved_box_without_showing_settings(self) -> None:
        class Root:
            withdrawn = False
            minimized = False

            def state(self) -> str:
                return "normal"

            def withdraw(self) -> None:
                self.withdrawn = True

            def iconify(self) -> None:
                self.minimized = True

            def update_idletasks(self) -> None:
                pass

        class Config:
            @staticmethod
            def get() -> dict[str, object]:
                return {"fixed_box": [10, 20, 310, 120]}

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app._overlay = None
        app._closed = False
        app.root = Root()
        app.config = Config()
        captured: list[tuple[list[int], str]] = []
        app.capture_box = lambda box, source, **_kwargs: captured.append((box, source))

        app.read_fixed_box(hide_settings=True)

        self.assertFalse(app.root.withdrawn)
        self.assertTrue(app.root.minimized)
        self.assertEqual(captured, [([10, 20, 310, 120], "Fixed box")])

    def test_explicit_tray_hide_is_respected_by_later_hotkeys(self) -> None:
        class Root:
            def state(self) -> str:
                return "withdrawn"

            def iconify(self) -> None:
                raise AssertionError("An explicitly hidden window was added back to the taskbar")

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app._closed = False
        app.root = Root()
        app.minimize_window()

    def test_structured_winocr_result_uses_only_readable_text(self) -> None:
        result = {
            "as_": {},
            "lines": [
                {
                    "as_": {},
                    "text": "Cricket 26 The Official Game of the",
                    "words": [{"bounding_rect": {"x": 15.0}, "text": "Cricket"}],
                }
            ],
            "text": "Cricket 26 The Official Game of the",
            "text_angle": -0.0,
        }

        self.assertEqual(
            OcrEngine.clean_text(OcrEngine._result_text(result)),
            "Cricket 26 The Official Game of the",
        )

    def test_config_reading_and_writing(self) -> None:
        test_root = Path(__file__).resolve().parent / "work"
        test_root.mkdir(exist_ok=True)
        path = test_root / "config_test.json"
        path.unlink(missing_ok=True)
        try:
            store = ConfigStore(path)
            self.assertEqual(store.load()["hotkeys"]["fixed"], "Alt+Z")
            saved = store.update(rate=7, volume=42, fixed_box=[10, 20, 310, 120], hotkeys={"snippet": "F3"})
            self.assertEqual(saved["fixed_box"], [10, 20, 310, 120])
            loaded = ConfigStore(path).load()
            self.assertEqual(loaded["rate"], 7)
            self.assertEqual(loaded["volume"], 42)
            self.assertEqual(loaded["hotkeys"]["snippet"], "F3")
        finally:
            path.unlink(missing_ok=True)

    def test_invalid_saved_hotkeys_fall_back_to_working_defaults(self) -> None:
        settings = validate_config({"hotkeys": {"fixed": None, "snippet": "   "}})

        self.assertEqual(settings["hotkeys"]["fixed"], "Alt+Z")
        self.assertEqual(settings["hotkeys"]["snippet"], "Alt+S")

    def test_an_explicitly_cleared_hotkey_stays_disabled(self) -> None:
        settings = validate_config({"hotkeys": {"fixed": "", "snippet": "Alt+S"}})

        self.assertEqual(settings["hotkeys"]["fixed"], "")

    def test_hotkey_modifier_order_is_canonical(self) -> None:
        self.assertEqual(
            to_pynput_hotkey("Ctrl+Alt+Z"),
            to_pynput_hotkey("Alt+Ctrl+Z"),
        )

    def test_custom_hotkeys_support_space_navigation_and_function_keys(self) -> None:
        self.assertEqual(normalise_hotkey("shift+ctrl+space"), "Ctrl+Shift+Space")
        self.assertEqual(to_pynput_hotkey("Ctrl+Shift+Space"), "<ctrl>+<shift>+<space>")
        self.assertEqual(normalise_hotkey("Alt+Page Down"), "Alt+PageDown")
        self.assertEqual(normalise_hotkey("Shift+F8"), "Shift+F8")

    def test_invalid_and_duplicate_shortcuts_are_rejected(self) -> None:
        with self.assertRaises(HotkeyError):
            normalise_hotkey("Q")
        with self.assertRaises(HotkeyError):
            normalise_hotkey("Ctrl+Alt")
        manager = HotkeyManager(lambda: None, lambda: None)
        with self.assertRaises(HotkeyError):
            manager.apply("Ctrl+Shift+Q", "Shift+Ctrl+Q")

        manager_with_replay = HotkeyManager(lambda: None, lambda: None, lambda: None)
        with self.assertRaises(HotkeyError):
            manager_with_replay.apply_all("Alt+X", "Alt+C", "Alt+X")

    def test_shortcut_recorder_builds_a_chord_from_pressed_keys(self) -> None:
        class Window:
            destroyed = False

            def destroy(self) -> None:
                self.destroyed = True

        class Prompt:
            def set(self, _value: str) -> None:
                pass

        recorder = _ShortcutRecorderDialog.__new__(_ShortcutRecorderDialog)
        recorder.result = None
        recorder._pressed_modifiers = {"Ctrl", "Shift"}
        recorder.window = Window()
        recorder.prompt = Prompt()

        recorder._key_pressed(SimpleNamespace(keysym="space", state=0))

        self.assertEqual(recorder.result, "Ctrl+Shift+Space")
        self.assertTrue(recorder.window.destroyed)

    def test_theme_preference_is_validated_and_persisted(self) -> None:
        self.assertEqual(validate_config({"theme": "dark"})["theme"], "dark")
        self.assertEqual(validate_config({"theme": "LIGHT"})["theme"], "light")
        self.assertEqual(validate_config({"theme": "neon"})["theme"], "system")

    def test_read_again_shortcut_is_optional_and_persisted(self) -> None:
        self.assertEqual(validate_config({})["hotkeys"]["read_again"], "")
        self.assertEqual(validate_config({"hotkeys": {"read_again": "Ctrl+Shift+R"}})["hotkeys"]["read_again"], "Ctrl+Shift+R")

    def test_native_title_bar_theme_can_be_applied_after_mapping(self) -> None:
        root = __import__("tkinter").Tk()
        try:
            root.update()
            self.assertTrue(apply_windows_title_bar(root, True))
            self.assertTrue(apply_windows_title_bar(root, False))
        finally:
            root.destroy()

    def test_ocr_settings_are_validated_without_breaking_old_configs(self) -> None:
        old_settings = validate_config({"voice": "legacy-voice"})
        requested = validate_config(
            {
                "ocr": {
                    "enabled": False,
                    "strength": "BALANCED",
                    "debug_logging": True,
                    "protected_words": ["Naytiba", "Mother Sphere", "", 42],
                    "replacements": [
                        {
                            "original": "Noytibos",
                            "replacement": "Naytibas",
                            "enabled": True,
                            "case_sensitive": False,
                            "whole_word": True,
                        }
                    ],
                }
            }
        )

        self.assertEqual(old_settings["ocr"]["strength"], "conservative")
        self.assertFalse(requested["ocr"]["enabled"])
        self.assertEqual(requested["ocr"]["strength"], "balanced")
        self.assertTrue(requested["ocr"]["debug_logging"])
        self.assertEqual(requested["ocr"]["protected_words"], ["Naytiba", "Mother Sphere"])
        self.assertEqual(requested["ocr"]["replacements"][0]["replacement"], "Naytibas")

    def test_capture_processing_keeps_raw_text_and_returns_corrected_speech_text(self) -> None:
        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.corrector = OcrCorrector()

        result = app.process_ocr_text(
            "In o second, Noytibos dealt with oll of them.",
            {
                "enabled": True,
                "strength": "conservative",
                "replacements": [
                    {
                        "original": "Noytibos",
                        "replacement": "Naytibas",
                        "enabled": True,
                        "case_sensitive": False,
                        "whole_word": True,
                    }
                ],
                "protected_words": [],
            },
        )

        self.assertEqual(result.raw_text, "In o second, Noytibos dealt with oll of them.")
        self.assertEqual(result.corrected_text, "In a second, Naytibas dealt with all of them.")

    def test_voice_discovery_never_calls_tk_from_its_worker_thread(self) -> None:
        class Tts:
            @staticmethod
            def list_voices() -> list[object]:
                return []

        class Root:
            calls = 0

            def after(self, *_args: object) -> None:
                self.calls += 1
                raise RuntimeError("Tk was called from a worker thread")

        ui = SettingsUI.__new__(SettingsUI)
        ui.tts = Tts()
        ui.root = Root()
        ui._voice_results = SimpleQueue()
        errors: list[Exception] = []

        def discover() -> None:
            try:
                ui._load_voices()
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=discover)
        worker.start()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(ui.root.calls, 0)
        self.assertEqual(ui._voice_results.get_nowait(), [])

    def test_settings_ui_builds_and_switches_theme(self) -> None:
        class SilentTts:
            @staticmethod
            def list_voices() -> list[object]:
                return []

            @staticmethod
            def stop() -> None:
                pass

            @staticmethod
            def speak(*_args: object) -> threading.Event:
                finished = threading.Event()
                finished.set()
                return finished

        path = Path(__file__).resolve().parent / "work" / "ui_config_test.json"
        path.unlink(missing_ok=True)
        root = __import__("tkinter").Tk()
        root.withdraw()
        try:
            store = ConfigStore(path)
            store.load()
            ui = SettingsUI(root, store, SilentTts(), lambda: None, lambda: None, lambda: None, lambda *_: None)
            root.update()
            self.assertEqual(len(ui.notebook.tabs()), 3)
            self.assertEqual(list(ui.profile_combo["values"]), ["Default"])
            self.assertTrue(ui.read_again_button.instate(["disabled"]))
            ui.set_read_again_enabled(True)
            self.assertFalse(ui.read_again_button.instate(["disabled"]))
            self.assertTrue(root.geometry().startswith("980x860"))
            ui.theme_value.set("Dark")
            ui._theme_changed()
            root.update()
            self.assertEqual(store.get()["theme"], "dark")
        finally:
            root.destroy()
            path.unlink(missing_ok=True)

    def test_settings_remain_reachable_at_150_percent_scaling(self) -> None:
        class SilentTts:
            @staticmethod
            def list_voices() -> list[object]:
                return []

        path = Path(__file__).resolve().parent / "work" / "ui_scale_test.json"
        path.unlink(missing_ok=True)
        root = __import__("tkinter").Tk()
        root.attributes("-alpha", 0.0)
        root.tk.call("tk", "scaling", 144 / 72)
        try:
            store = ConfigStore(path)
            store.load()
            ui = SettingsUI(root, store, SilentTts(), lambda: None, lambda: None, lambda: None, lambda *_: None)
            root.update()
            ui.notebook.select(2)
            root.update_idletasks()
            region = ui.settings_canvas.bbox("all")
            self.assertIsNotNone(region)
            self.assertGreater(region[3], ui.settings_canvas.winfo_height())
            ui.settings_canvas.yview_moveto(1.0)
            root.update_idletasks()
            canvas_top = ui.settings_canvas.winfo_rooty()
            canvas_bottom = canvas_top + ui.settings_canvas.winfo_height()
            button_top = ui.apply_shortcuts_button.winfo_rooty()
            button_bottom = button_top + ui.apply_shortcuts_button.winfo_height()
            self.assertGreaterEqual(button_top, canvas_top)
            self.assertLessEqual(button_bottom, canvas_bottom)
        finally:
            root.destroy()
            path.unlink(missing_ok=True)

    def test_stop_interrupts_current_speech_request(self) -> None:
        class InterruptibleTts(TtsEngine):
            def __init__(self) -> None:
                self.started = threading.Event()
                self.fallback_cancel = threading.Event()
                super().__init__()

            def _play(self, request: object) -> None:
                self.started.set()
                getattr(request, "cancel", self.fallback_cancel).wait(5)

        engine = InterruptibleTts()
        try:
            finished = engine.speak("A deliberately long sentence")
            self.assertTrue(engine.started.wait(1))
            engine.stop()
            self.assertTrue(finished.wait(0.5), "Stop did not interrupt the active utterance")
        finally:
            engine.fallback_cancel.set()
            engine.shutdown()

    def test_native_winocr_recognition(self) -> None:
        image = Image.new("RGB", (760, 150), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("arial.ttf", 48)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 45), "Native OCR Test 123", fill="black", font=font)
        engine = OcrEngine()
        try:
            recognised = engine.recognise(image)
        except OcrError as exc:
            self.fail(f"Windows Media OCR did not complete: {exc}")
        finally:
            engine.close()
        self.assertIsInstance(recognised, str)
        self.assertTrue(recognised.strip(), "Windows OCR returned no text for the generated test image")

    def test_ocr_session_is_reused_across_repeated_captures(self) -> None:
        created = 0

        class Session:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = 0

            def recognise(self, _image: object) -> dict[str, str]:
                self.calls += 1
                return {"text": f"Result {self.calls}"}

            def close(self) -> None:
                self.closed += 1

        session = Session()

        def factory(_language: str) -> Session:
            nonlocal created
            created += 1
            return session

        engine = OcrEngine(session_factory=factory)
        image = Image.new("RGB", (20, 20), "white")
        engine.warm_up()
        self.assertEqual(engine.recognise(image), "Result 1")
        self.assertEqual(engine.recognise(image), "Result 2")
        engine.close()

        self.assertEqual(created, 1)
        self.assertEqual(session.closed, 1)

    def test_tts_playback_through_default_speakers(self) -> None:
        engine = TtsEngine()
        try:
            finished = engine.speak("Game Text Reader audio test.")
            self.assertTrue(finished.wait(20), "Timed out waiting for Windows speech playback")
            if engine.last_error and "-2147200966" in str(engine.last_error):
                self.skipTest("The current Windows session has no available default audio endpoint.")
            self.assertIsNone(engine.last_error, f"Windows speech reported an error: {engine.last_error}")
        finally:
            engine.shutdown()

    def test_global_hotkey_registration(self) -> None:
        fired = threading.Event()
        manager = HotkeyManager(fired.set, lambda: None)
        try:
            fixed, snippet = manager.apply("Ctrl+Alt+F10", "Ctrl+Alt+F11")
            self.assertEqual(fixed, "Ctrl+Alt+F10")
            self.assertEqual(snippet, "Ctrl+Alt+F11")
            self.assertTrue(manager.is_running)
            user32 = ctypes.windll.user32
            for virtual_key in (0x11, 0x12, 0x79):
                user32.keybd_event(virtual_key, 0, 0, 0)
            for virtual_key in (0x79, 0x12, 0x11):
                user32.keybd_event(virtual_key, 0, 2, 0)
            self.assertTrue(fired.wait(1), "The registered shortcut did not dispatch its callback")
        finally:
            manager.stop()
        self.assertFalse(manager.is_running)

    def test_windows_reports_a_shortcut_owned_by_another_registration(self) -> None:
        first = HotkeyManager(lambda: None, lambda: None)
        second = HotkeyManager(lambda: None, lambda: None)
        try:
            first.apply("Ctrl+Alt+Shift+F10", "")
            with self.assertRaisesRegex(HotkeyError, "already assigned"):
                second.apply("Ctrl+Alt+Shift+F10", "")
        finally:
            second.stop()
            first.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
