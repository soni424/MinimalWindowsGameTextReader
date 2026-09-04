"""Automated smoke tests for the native Windows Game Text Reader components.

Run with ``python test_app.py`` on Windows.  The speech test deliberately plays
a short phrase through the configured default Windows speaker.
"""

from __future__ import annotations

import threading
import unittest
import ctypes
import inspect
import sys
from pathlib import Path
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

import settings_ui as settings_ui_module
from app_resources import (
    APP_ICON_ICO_PATH,
    APP_ICON_MASTER_PATH,
    APP_ICON_WINDOW_PATH,
    apply_window_icon,
)
from appearance import DARK, LIGHT, apply_windows_title_bar
from config import ConfigStore, validate_config
from hotkey_manager import HotkeyError, HotkeyManager, normalise_hotkey, to_pynput_hotkey
from main import GameTextReaderApplication
from ocr_correction import CorrectionResult, OcrCorrector
from ocr_engine import OcrEngine, OcrError
from overlay import QuickSnippetOverlay
from settings_ui import SettingsUI, _ShortcutRecorderDialog
from tray_app import TrayApp
from tts_engine import TtsEngine, _WindowsSpeechSession


@unittest.skipUnless(__import__("os").name == "nt", "Windows native APIs are required")
class WindowsComponentTests(unittest.TestCase):
    """Exercise the four native integration points without launching the GUI."""

    def test_app_icon_assets_cover_window_tray_and_packaging(self) -> None:
        for path in (APP_ICON_MASTER_PATH, APP_ICON_WINDOW_PATH, APP_ICON_ICO_PATH):
            self.assertTrue(path.is_file(), f"Missing icon asset: {path}")

        with Image.open(APP_ICON_MASTER_PATH) as master:
            self.assertEqual(master.size, (1024, 1024))
            self.assertEqual(master.mode, "RGBA")
        with Image.open(APP_ICON_ICO_PATH) as windows_icon:
            self.assertTrue(
                {(16, 16), (32, 32), (48, 48), (256, 256)}.issubset(windows_icon.ico.sizes())
            )

        tray_icon = TrayApp._make_icon_image()
        self.assertEqual(tray_icon.size, (64, 64))
        self.assertEqual(tray_icon.mode, "RGBA")

        spec = (Path(__file__).resolve().parent / "GameTextReader.spec").read_text(encoding="utf-8")
        self.assertIn('datas = [("assets", "assets")]', spec)
        self.assertIn('icon="assets/app_icon.ico"', spec)
        self.assertIn('manifest="assets/GameTextReader.manifest"', spec)

    def test_tray_start_reports_success_and_failure(self) -> None:
        class Menu:
            SEPARATOR = object()

            def __init__(self, *_items: object) -> None:
                pass

        class Icon:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def run_detached(self) -> None:
                pass

        fake_pystray = SimpleNamespace(
            Icon=Icon,
            Menu=Menu,
            MenuItem=lambda *_args, **_kwargs: object(),
        )
        tray = TrayApp(lambda: None, lambda: None, lambda: None, lambda: None, lambda: None)
        with patch.dict(sys.modules, {"pystray": fake_pystray}):
            self.assertTrue(tray.start())

        failing_pystray = SimpleNamespace(
            Icon=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("blocked")),
            Menu=Menu,
            MenuItem=lambda *_args, **_kwargs: object(),
        )
        with patch.dict(sys.modules, {"pystray": failing_pystray}):
            self.assertFalse(tray.start())

    def test_overlay_uses_one_exact_pane_per_monitor(self) -> None:
        from capture_profiles import MonitorInfo

        root = __import__("tkinter").Tk()
        root.withdraw()
        monitors = [
            MonitorInfo("DISPLAY1", 0, 0, 960, 1080, 96, 96, True),
            MonitorInfo("DISPLAY2", 960, 0, 1920, 1080, 144, 144, False),
        ]
        overlay = QuickSnippetOverlay(
            root,
            lambda _box: None,
            lambda: None,
            monitor_provider=lambda: monitors,
        )
        try:
            root.update()
            self.assertEqual(len(overlay.panes), 2)
            for pane in overlay.panes:
                self.assertEqual(pane.canvas.winfo_width(), pane.monitor.width)
                self.assertEqual(pane.canvas.winfo_height(), pane.monitor.height)
            external = overlay.panes[1]
            self.assertEqual(overlay.screen_to_canvas(1200, 400, external), (240.0, 400.0))
            self.assertEqual(overlay.canvas_to_screen(240, 400, external), (1200, 400))
        finally:
            overlay.close()
            root.update()
            root.destroy()

    def test_main_window_accepts_the_branded_icon(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            self.assertTrue(apply_window_icon(root))
            self.assertIsNotNone(getattr(root, "_game_text_reader_icon", None))
        finally:
            root.destroy()

    def test_profile_dialogs_use_the_current_theme_instead_of_native_prompts(self) -> None:
        self.assertTrue(hasattr(settings_ui_module, "_ThemedTextPrompt"))
        self.assertTrue(hasattr(settings_ui_module, "_ThemedConfirmDialog"))
        self.assertNotIn("simpledialog.askstring", inspect.getsource(SettingsUI.create_profile))
        self.assertNotIn("simpledialog.askstring", inspect.getsource(SettingsUI.rename_profile))
        self.assertNotIn("messagebox.askyesno", inspect.getsource(SettingsUI.delete_profile))

    def test_profile_prompt_body_controls_and_title_bar_follow_each_palette(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.withdraw()
        ui = SettingsUI.__new__(SettingsUI)
        ui.root = root
        ui.style = ttk.Style(root)
        try:
            ui.style.theme_use("clam")
            for palette in (DARK, LIGHT):
                ui._configure_styles(palette)
                with patch("settings_ui.apply_windows_title_bar", return_value=True) as apply_title_bar:
                    dialog = settings_ui_module._ThemedTextPrompt(
                        root,
                        palette,
                        "New capture profile",
                        "Profile name:",
                    )
                    dialog.window.deiconify()
                    root.update()
                    self.assertEqual(dialog.window.cget("background"), palette.window)
                    self.assertEqual(ui.style.lookup("Card.TFrame", "background"), palette.card)
                    self.assertEqual(ui.style.lookup("CardText.TLabel", "foreground"), palette.text)
                    self.assertEqual(ui.style.lookup("TEntry", "fieldbackground"), palette.input)
                    self.assertEqual(ui.style.lookup("TEntry", "foreground"), palette.text)
                    self.assertEqual(ui.style.lookup("Primary.TButton", "background"), palette.accent)
                    self.assertTrue(dialog.window.bind("<Return>"))
                    self.assertTrue(dialog.window.bind("<Escape>"))
                    self.assertTrue(apply_title_bar.called)
                    self.assertEqual(apply_title_bar.call_args.args[1], palette.dark)
                    dialog._cancel()
        finally:
            root.destroy()

    def test_profile_prompt_uses_the_latest_runtime_palette_and_preserves_actions(self) -> None:
        created: list[str] = []
        captured_palettes: list[object] = []
        prompt_results = iter(("Dark profile", None, "Light profile"))

        class Prompt:
            def __init__(self, _root: object, palette: object, *_args: object, **_kwargs: object) -> None:
                captured_palettes.append(palette)

            @staticmethod
            def show() -> str | None:
                return next(prompt_results)

        ui = SettingsUI.__new__(SettingsUI)
        ui.root = object()
        ui._palette = DARK
        ui.on_profile_create = created.append
        ui.set_status = lambda *_args, **_kwargs: None
        with patch("settings_ui._ThemedTextPrompt", Prompt):
            ui.create_profile()
            ui.create_profile()
            ui._palette = LIGHT
            ui.create_profile()

        self.assertEqual(created, ["Dark profile", "Light profile"])
        self.assertEqual(captured_palettes, [DARK, DARK, LIGHT])

    def test_profile_delete_confirmation_keeps_cancel_and_confirm_behaviour(self) -> None:
        deleted: list[str] = []

        class Confirmation:
            result = False

            def __init__(self, _root: object, palette: object, *_args: object, **_kwargs: object) -> None:
                self.palette = palette

            def show(self) -> bool:
                return self.result

        ui = SettingsUI.__new__(SettingsUI)
        ui.root = object()
        ui._palette = DARK
        ui._profile_id_by_name = {"Default": "default"}
        ui.profile_value = SimpleNamespace(get=lambda: "Default")
        ui.on_profile_delete = deleted.append
        ui.set_status = lambda *_args, **_kwargs: None
        with patch("settings_ui._ThemedConfirmDialog", Confirmation):
            ui.delete_profile()
            self.assertEqual(deleted, [])
            Confirmation.result = True
            ui.delete_profile()

        self.assertEqual(deleted, ["default"])

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

    def test_startup_preference_is_backward_compatible_and_validated(self) -> None:
        self.assertFalse(validate_config({})["startup"]["enabled"])
        self.assertTrue(validate_config({"startup": {"enabled": True}})["startup"]["enabled"])
        self.assertFalse(validate_config({"startup": {"enabled": "yes"}})["startup"]["enabled"])

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

    def test_combobox_popdowns_follow_palette_for_all_interaction_states(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.attributes("-alpha", 0.0)
        try:
            ui = SettingsUI.__new__(SettingsUI)
            ui.root = root
            ui.style = ttk.Style(root)
            ui.style.theme_use("clam")
            ui._comboboxes = []
            ui._palette = DARK
            ui._configure_styles(DARK)
            container = ttk.Frame(root)
            container.pack(fill="x")
            for values in (("System", "Dark", "Light"), ("Default", "Persona 3 Reload"), ("Conservative", "Balanced", "Strong"), ("Microsoft David", "Microsoft Zira")):
                combo = ui._register_combobox(ttk.Combobox(container, values=values, state="readonly"))
                combo.current(0)
                combo.pack(fill="x")
            root.update()

            def descendants(widget: tk.Misc) -> list[tk.Misc]:
                children: list[tk.Misc] = []
                for child in widget.winfo_children():
                    children.append(child)
                    children.extend(descendants(child))
                return children

            combos = [widget for widget in descendants(root) if isinstance(widget, ttk.Combobox)]
            self.assertGreaterEqual(len(combos), 3)

            for palette in (DARK, LIGHT):
                ui._palette = palette
                ui._configure_styles(palette)
                ui._apply_tk_colours(palette)
                root.update()
                self.assertEqual(
                    ui.style.lookup("TCombobox", "background", state=("active",)),
                    palette.button_hover,
                )
                self.assertEqual(
                    ui.style.lookup("TCombobox", "background", state=("pressed",)),
                    palette.button_hover,
                )
                self.assertEqual(
                    ui.style.lookup("TCombobox", "background", state=("disabled",)),
                    palette.card_alt,
                )
                self.assertEqual(
                    ui.style.lookup("TCombobox", "arrowcolor", state=("disabled",)),
                    palette.muted,
                )
                self.assertEqual(ui.style.lookup("ComboboxPopdownFrame", "background"), palette.input)
                self.assertEqual(ui.style.lookup("Vertical.TScrollbar", "background"), palette.button)
                self.assertEqual(ui.style.lookup("Vertical.TScrollbar", "troughcolor"), palette.input)
                self.assertEqual(ui.style.lookup("Vertical.TScrollbar", "lightcolor"), palette.button)

                for combo in combos:
                    root.tk.call("ttk::combobox::Post", combo._w)
                    root.update()
                    popdown = root.tk.call("ttk::combobox::PopdownWindow", combo._w)
                    listbox = f"{popdown}.f.l"
                    self.assertEqual(root.tk.call(listbox, "cget", "-background"), palette.input)
                    self.assertEqual(root.tk.call(listbox, "cget", "-foreground"), palette.text)
                    self.assertEqual(root.tk.call(listbox, "cget", "-selectbackground"), palette.selection)
                    self.assertEqual(root.tk.call(listbox, "cget", "-selectforeground"), palette.text)
                    self.assertEqual(root.tk.call(listbox, "cget", "-highlightbackground"), palette.border)
                    self.assertEqual(root.tk.call(listbox, "cget", "-highlightcolor"), palette.accent)
                    root.tk.call("ttk::combobox::Unpost", combo._w)

            # Existing popdowns are refreshed immediately when the palette changes.
            combo = combos[0]
            ui._palette = DARK
            ui._configure_styles(DARK)
            root.tk.call("ttk::combobox::Post", combo._w)
            root.update()
            listbox = f"{root.tk.call('ttk::combobox::PopdownWindow', combo._w)}.f.l"
            ui._palette = LIGHT
            ui._configure_styles(LIGHT)
            root.update()
            self.assertEqual(root.tk.call(listbox, "cget", "-background"), LIGHT.input)
            self.assertEqual(root.tk.call(listbox, "cget", "-selectbackground"), LIGHT.selection)
            root.tk.call("ttk::combobox::Unpost", combo._w)
        finally:
            root.destroy()

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
            self.assertEqual(ui.capture_speech_value.get(), "Replace current line")
            ui.capture_speech_value.set("Queue next line")
            ui._save_capture_speech_settings()
            self.assertEqual(store.get()["speech"]["capture_mode"], "queue")
            ui.capture_speech_value.set("Allow overlapping lines")
            ui.capture_overlap_value.set(3)
            ui._save_capture_speech_settings()
            self.assertEqual(store.get()["speech"], {"capture_mode": "overlap", "max_overlap": 3})
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

    def test_voice_preview_caps_only_the_preview_volume(self) -> None:
        calls: list[tuple[object, ...]] = []
        statuses: list[str] = []

        class Tts:
            def stop(self) -> None:
                calls.append(("stop",))

            def speak(self, *args: object) -> None:
                calls.append(args)

        ui = SettingsUI.__new__(SettingsUI)
        ui.tts = Tts()
        ui._save_voice_settings = lambda: None
        ui.speech_settings = lambda: ("sapi:test", 0, 100)
        ui.set_status = statuses.append

        ui.test_voice()
        ui._preview_speech("Naytibas", "replacement preview")

        self.assertEqual(calls[0], ("stop",))
        self.assertEqual(calls[1], ("This is your selected Windows voice.", "sapi:test", 0, 60))
        self.assertEqual(calls[2], ("stop",))
        self.assertEqual(calls[3], ("Naytibas", "sapi:test", 0, 60))
        self.assertIn("preview volume", statuses[0])
        self.assertIn("replacement preview", statuses[1])

    def test_replacement_preview_uses_only_unsaved_replacement_text(self) -> None:
        root = __import__("tkinter").Tk()
        root.withdraw()
        previews: list[str] = []
        try:
            with patch.object(settings_ui_module, "_show_modal", lambda *_args: None):
                dialog = settings_ui_module._ReplacementDialog(
                    root,
                    DARK,
                    {"original": "Stupei", "replacement": "Stupey"},
                    previews.append,
                )
            root.update_idletasks()

            self.assertFalse(dialog.original_play_button.instate(["disabled"]))
            self.assertFalse(dialog.replacement_play_button.instate(["disabled"]))
            dialog._play_original()
            dialog._play_replacement()
            dialog.original.set("Noytibos")
            dialog.replacement.set("Naytibas")
            dialog._play_original()
            dialog._play_replacement()

            self.assertEqual(previews, ["Stupei", "Stupey", "Noytibos", "Naytibas"])
            self.assertIsNone(dialog.result)

            dialog.original.set("   ")
            dialog.replacement.set("   ")
            root.update_idletasks()
            self.assertTrue(dialog.original_play_button.instate(["disabled"]))
            self.assertTrue(dialog.replacement_play_button.instate(["disabled"]))
            dialog.window.destroy()
        finally:
            root.destroy()

    def test_typed_text_becomes_replayable_and_invalidates_ocr_details(self) -> None:
        class SilentTts:
            @staticmethod
            def list_voices() -> list[object]:
                return []

        manual_text: list[str] = []
        path = Path(__file__).resolve().parent / "work" / "ui_manual_text_test.json"
        path.unlink(missing_ok=True)
        root = __import__("tkinter").Tk()
        root.withdraw()
        try:
            store = ConfigStore(path)
            store.load()
            ui = SettingsUI(
                root,
                store,
                SilentTts(),
                lambda: None,
                lambda: None,
                lambda: None,
                lambda *_: None,
                on_manual_text_changed=manual_text.append,
            )
            ui.set_last_result(CorrectionResult("Raw", "Corrected", (), 0.0))
            self.assertFalse(ui.details_button.instate(["disabled"]))

            ui.captured_text.insert("end", " plus typed text")
            root.update()

            self.assertEqual(manual_text[-1], "Corrected plus typed text")
            self.assertFalse(ui.read_again_button.instate(["disabled"]))
            self.assertTrue(ui.details_button.instate(["disabled"]))
            self.assertIsNone(ui._last_result)
            self.assertIn("Edited text", ui.capture_meta.get())

            ui.captured_text.delete("1.0", "end")
            root.update()
            self.assertEqual(manual_text[-1], "")
            self.assertTrue(ui.read_again_button.instate(["disabled"]))
        finally:
            root.destroy()
            path.unlink(missing_ok=True)

    def test_startup_checkbox_rolls_back_when_windows_rejects_the_change(self) -> None:
        root = __import__("tkinter").Tk()
        root.withdraw()
        statuses: list[tuple[str, bool]] = []

        class Alert:
            def __init__(self, *_args: object) -> None:
                pass

            def show(self) -> None:
                pass

        ui = SettingsUI.__new__(SettingsUI)
        ui.root = root
        ui._palette = DARK
        ui.startup_enabled = __import__("tkinter").BooleanVar(root, value=True)
        ui.on_startup_changed = lambda _enabled: (_ for _ in ()).throw(
            RuntimeError("Startup access was blocked")
        )
        ui.set_status = lambda message, error=False: statuses.append((message, error))
        try:
            with patch.object(settings_ui_module, "_ThemedAlertDialog", Alert):
                ui._startup_changed()
            self.assertFalse(ui.startup_enabled.get())
            self.assertEqual(statuses, [("Startup access was blocked", True)])
        finally:
            root.destroy()

    def test_voice_and_shortcuts_cards_reflow_at_dpi_adjusted_breakpoint(self) -> None:
        class SilentTts:
            @staticmethod
            def list_voices() -> list[object]:
                return []

        path = Path(__file__).resolve().parent / "work" / "ui_reflow_test.json"
        path.unlink(missing_ok=True)
        root = __import__("tkinter").Tk()
        root.withdraw()
        try:
            store = ConfigStore(path)
            store.load()
            ui = SettingsUI(root, store, SilentTts(), lambda: None, lambda: None, lambda: None, lambda *_: None)
            root.update_idletasks()
            breakpoint = round(1200 * max(1.0, root.winfo_fpixels("1i") / 96.0))

            ui._layout_settings_cards(breakpoint - 1)
            self.assertEqual(int(ui.voice_card.grid_info()["row"]), 0)
            self.assertEqual(int(ui.shortcuts_card.grid_info()["row"]), 1)
            self.assertEqual(int(ui.voice_card.grid_info()["columnspan"]), 2)

            ui._layout_settings_cards(breakpoint + 1)
            self.assertEqual(int(ui.voice_card.grid_info()["column"]), 0)
            self.assertEqual(int(ui.shortcuts_card.grid_info()["column"]), 1)
            self.assertEqual(int(ui.shortcuts_card.grid_info()["row"]), 0)

            ui._layout_settings_cards(breakpoint - 1)
            self.assertEqual(int(ui.shortcuts_card.grid_info()["row"]), 1)
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

    def test_sapi_wav_header_matches_the_requested_pcm_format(self) -> None:
        import io
        import wave

        import pythoncom
        import win32com.client

        from tts_engine import _SpeechRequest, _WindowsSpeechSession

        pythoncom.CoInitialize()
        session = _WindowsSpeechSession()
        audio_format = None
        wave_format = None
        try:
            audio_format = win32com.client.Dispatch("SAPI.SpAudioFormat")
            audio_format.Type = session._SAPI_FORMAT_TYPE
            wave_format = audio_format.GetWaveFormatEx()
            expected = (
                int(wave_format.SamplesPerSec),
                int(wave_format.BitsPerSample),
                int(wave_format.Channels),
            )
            self.assertEqual(expected, (22050, 16, 1))
            request = _SpeechRequest(
                request_id=1,
                text="SAPI format test",
                voice_id="",
                rate=0,
                volume=20,
            )
            wav_data = session._synthesise_sapi_wav(request)
            with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
                actual = (
                    wav_file.getframerate(),
                    wav_file.getsampwidth() * 8,
                    wav_file.getnchannels(),
                )
            self.assertEqual(
                actual,
                expected,
                "SAPI PCM bytes and the WAV header use different formats",
            )
        finally:
            session.close()
            del wave_format, audio_format
            pythoncom.CoUninitialize()

    def test_media_player_replacement_retains_stream_until_handoff(self) -> None:
        class Stream:
            content_type = "audio/wav"

            def __init__(self, name: str) -> None:
                self.name = name
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class Player:
            def __init__(self) -> None:
                self.source = None
                self.play_count = 0
                self.pause_count = 0
                self.volume = 1.0

            def play(self) -> None:
                self.play_count += 1

            def pause(self) -> None:
                self.pause_count += 1

        session = _WindowsSpeechSession.__new__(_WindowsSpeechSession)
        old_stream = Stream("old")
        new_stream = Stream("new")
        old_player = Player()
        new_player = Player()
        old_channel = type("Channel", (), {})()
        old_channel.player = old_player
        old_channel.stream = old_stream
        old_channel.source = None
        old_channel.finished = threading.Event()
        old_channel.failed = threading.Event()
        old_channel.error = ""
        old_channel.retired = False
        session._winrt_current_channel = old_channel
        session._winrt_retired_channels = []
        session._winrt_player = old_player
        session._winrt_loop = object()
        session._winrt_deadline = 0.0

        # The test uses a fake channel factory so the handoff itself stays
        # deterministic and never depends on a physical audio device.
        class ChannelFactory:
            def __call__(_self: object) -> object:
                channel = type("Channel", (), {})()
                channel.player = new_player
                channel.stream = None
                channel.source = None
                channel.finished = threading.Event()
                channel.failed = threading.Event()
                channel.error = ""
                channel.retired = False
                return channel

        session._new_playback_channel = ChannelFactory()  # type: ignore[method-assign]

        class SourceSetter:
            @staticmethod
            def set_media_stream(channel: object, stream: object) -> None:
                channel.player.source = stream

        session._set_media_stream = SourceSetter.set_media_stream  # type: ignore[method-assign]
        session._start_stream(new_stream, lambda: None, True, 1.0)

        self.assertFalse(
            old_stream.closed,
            "the previous stream was closed during the asynchronous handoff",
        )
        self.assertEqual(old_player.pause_count, 1)
        self.assertEqual(old_player.volume, 0.0)
        self.assertEqual(new_player.play_count, 1)
        self.assertEqual(len(session._winrt_retired_channels), 1)
        session._winrt_retired_channels = [
            (channel, 0.0)
            for channel, _retire_at in session._winrt_retired_channels
        ]
        session._close_retired_channels()
        self.assertTrue(old_stream.closed)

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
