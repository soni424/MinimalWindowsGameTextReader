"""Regression tests for persistent windows, capture profiles, and runtime jobs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from capture_profiles import (
    CaptureProfileManager,
    MonitorInfo,
    build_capture_area,
    resolve_capture_area,
)
from capture_pipeline import CaptureJob, CaptureWorker, PipelineTimings
from config import CONFIG_PATH, ConfigImportError, ConfigStore, validate_config
import main as main_module
from main import GameTextReaderApplication
from ocr_correction import CorrectionResult
from reader_state import ReaderTextState
from speech_text import prepare_for_speech
from startup_registration import StartupRegistrationError
from tts_engine import TtsEngine
from window_state import WindowPlacement, WindowStateController, restore_window_placement


class ConfigurationMigrationTests(unittest.TestCase):
    def test_default_settings_path_is_stable_local_app_data(self) -> None:
        self.assertEqual(
            CONFIG_PATH,
            Path(os.environ["LOCALAPPDATA"]) / "GameTextReader" / "config.json",
        )

    def test_portable_settings_are_migrated_without_deleting_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "old" / "_internal" / "config.json"
            target = root / "AppData" / "GameTextReader" / "config.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                json.dumps({"theme": "dark", "hotkeys": {"fixed": "Alt+Q"}}),
                encoding="utf-8",
            )

            store = ConfigStore(target, legacy_paths=(legacy,))
            loaded = store.load()

            self.assertEqual(loaded["theme"], "dark")
            self.assertEqual(loaded["hotkeys"]["fixed"], "Alt+Q")
            self.assertEqual(store.migrated_from, legacy)
            self.assertTrue(legacy.exists())
            self.assertTrue(target.exists())

    def test_existing_app_data_wins_over_legacy_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "config.json"
            legacy = root / "old-config.json"
            target.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
            legacy.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

            store = ConfigStore(target, legacy_paths=(legacy,))

            self.assertEqual(store.load()["theme"], "light")
            self.assertIsNone(store.migrated_from)

    def test_manual_old_folder_import_validates_and_backs_up_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "current" / "config.json"
            old_config = root / "old-app" / "_internal" / "config.json"
            old_config.parent.mkdir(parents=True)
            old_config.write_text(
                json.dumps({"theme": "dark", "voice": "old-voice"}),
                encoding="utf-8",
            )
            store = ConfigStore(target)
            store.load()
            store.update(theme="light", voice="current-voice")

            source = store.import_from_older_app_folder(root / "old-app")

            self.assertEqual(source, old_config)
            self.assertEqual(store.get()["voice"], "old-voice")
            backup = target.with_name("config.before-import.json")
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["voice"], "current-voice")

    def test_invalid_manual_import_does_not_change_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "current" / "config.json"
            old_config = root / "old-app" / "config.json"
            old_config.parent.mkdir(parents=True)
            old_config.write_text("not json", encoding="utf-8")
            store = ConfigStore(target)
            store.load()
            store.update(theme="dark")

            with self.assertRaises(ConfigImportError):
                store.import_from_older_app_folder(root / "old-app")

            self.assertEqual(store.get()["theme"], "dark")

    def test_legacy_fixed_box_becomes_the_default_capture_profile(self) -> None:
        settings = validate_config(
            {
                "voice": "legacy-voice",
                "fixed_box": [100, 200, 700, 420],
                "hotkeys": {"fixed": "Alt+X", "snippet": "Alt+C"},
            }
        )

        self.assertEqual(settings["voice"], "legacy-voice")
        self.assertEqual(settings["selected_profile_id"], "default")
        self.assertEqual(len(settings["capture_profiles"]), 1)
        profile = settings["capture_profiles"][0]
        self.assertEqual(profile["name"], "Default")
        self.assertEqual(profile["capture_area"]["box"], [100, 200, 700, 420])
        self.assertEqual(profile["settings"], {})

    def test_new_window_settings_are_validated_without_resetting_other_values(self) -> None:
        settings = validate_config(
            {
                "theme": "dark",
                "rate": 4,
                "window": {
                    "width": 1110,
                    "height": 870,
                    "x": -1320,
                    "y": 45,
                    "state": "maximized",
                },
            }
        )

        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["rate"], 4)
        self.assertEqual(settings["speech"]["capture_mode"], "replace")
        self.assertEqual(settings["speech"]["max_overlap"], 2)
        self.assertEqual(
            settings["window"],
            {"width": 1110, "height": 870, "x": -1320, "y": 45, "state": "maximized"},
        )

    def test_rapid_capture_mode_is_validated_and_persisted(self) -> None:
        self.assertEqual(
            validate_config({"speech": {"capture_mode": "replace"}})["speech"],
            {"capture_mode": "replace", "max_overlap": 2},
        )
        self.assertEqual(
            validate_config({"speech": {"capture_mode": "unknown"}})["speech"],
            {"capture_mode": "replace", "max_overlap": 2},
        )
        self.assertEqual(
            validate_config({"speech": {"capture_mode": "overlap", "max_overlap": 9}})["speech"],
            {"capture_mode": "overlap", "max_overlap": 4},
        )


class CaptureProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_monitor = MonitorInfo(
            device=r"\\.\DISPLAY2",
            left=1920,
            top=0,
            right=3840,
            bottom=1080,
            dpi_x=96,
            dpi_y=96,
            primary=False,
        )

    def test_region_tracks_the_same_monitor_after_resolution_and_layout_change(self) -> None:
        area = build_capture_area([2020, 100, 3020, 300], [self.original_monitor])
        moved_monitor = MonitorInfo(
            device=r"\\.\DISPLAY2",
            left=0,
            top=-1440,
            right=2560,
            bottom=0,
            dpi_x=144,
            dpi_y=144,
            primary=False,
        )

        resolved = resolve_capture_area(area, [moved_monitor])

        self.assertTrue(resolved.available)
        self.assertEqual(resolved.box, [133, -1307, 1467, -1040])

    def test_region_is_not_silently_moved_when_its_monitor_is_unavailable(self) -> None:
        area = build_capture_area([2020, 100, 3020, 300], [self.original_monitor])
        primary = MonitorInfo("primary", 0, 0, 1920, 1080, 96, 96, True)

        resolved = resolve_capture_area(area, [primary])

        self.assertFalse(resolved.available)
        self.assertIsNone(resolved.box)
        self.assertIn("not connected", resolved.reason.lower())

    def test_profile_crud_persists_and_keeps_the_legacy_box_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary) / "config.json")
            store.load()
            manager = CaptureProfileManager(store, monitor_provider=lambda: [self.original_monitor])

            game_a = manager.create("Game A")
            manager.update_selected_area([2020, 100, 3020, 300])
            game_b = manager.create("Game B")
            manager.update_selected_area([2100, 400, 3300, 700])
            manager.select(game_a["id"])
            manager.rename(game_a["id"], "Game A Remastered")

            reloaded = ConfigStore(store.path)
            reloaded.load()
            restored = CaptureProfileManager(reloaded, monitor_provider=lambda: [self.original_monitor])

            self.assertEqual(restored.selected["name"], "Game A Remastered")
            self.assertEqual(restored.resolve_selected().box, [2020, 100, 3020, 300])
            self.assertEqual(reloaded.get()["fixed_box"], [2020, 100, 3020, 300])
            restored.delete(game_b["id"])
            self.assertEqual([item["name"] for item in restored.profiles], ["Default", "Game A Remastered"])


class WindowPlacementTests(unittest.TestCase):
    def test_valid_negative_monitor_position_is_preserved(self) -> None:
        monitor = MonitorInfo("left", -1920, 0, 0, 1080, 96, 96, False)
        saved = WindowPlacement(980, 820, -1700, 80, "normal")

        restored = restore_window_placement(saved, [monitor])

        self.assertEqual(restored, saved)

    def test_controller_persists_user_geometry_and_ignores_minimize(self) -> None:
        import tkinter as tk

        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary) / "config.json")
            store.load()
            root = tk.Tk()
            root.attributes("-alpha", 0.0)
            controller = WindowStateController(root, store)
            try:
                root.update()
                root.geometry("800x720+100+80")
                root.update()
                controller.persist_now()
                preferred = store.get()["window"]
                self.assertEqual(preferred, {"width": 800, "height": 720, "x": 100, "y": 80, "state": "normal"})

                root.iconify()
                root.update_idletasks()
                controller.persist_now()
                self.assertEqual(store.get()["window"], preferred)
            finally:
                controller.close()
                root.destroy()

            restored_root = tk.Tk()
            restored_root.attributes("-alpha", 0.0)
            reloaded_store = ConfigStore(store.path)
            reloaded_store.load()
            restored = WindowStateController(restored_root, reloaded_store)
            try:
                restored_root.update()
                self.assertEqual(restored_root.geometry(), "800x720+100+80")
            finally:
                restored.close()
                restored_root.destroy()

    def test_controller_restores_maximized_state_without_losing_normal_geometry(self) -> None:
        import tkinter as tk

        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary) / "config.json")
            store.load()
            root = tk.Tk()
            root.attributes("-alpha", 0.0)
            controller = WindowStateController(root, store)
            try:
                root.update()
                root.geometry("820x730+120+60")
                root.update()
                root.state("zoomed")
                root.update()
                controller.persist_now()
                saved = store.get()["window"]
                self.assertEqual(saved["state"], "maximized")
                self.assertEqual((saved["width"], saved["height"], saved["x"], saved["y"]), (820, 730, 120, 60))
            finally:
                controller.close()
                root.destroy()

            reloaded = ConfigStore(store.path)
            reloaded.load()
            restored_root = tk.Tk()
            restored_root.attributes("-alpha", 0.0)
            restored = WindowStateController(restored_root, reloaded)
            try:
                restored_root.update()
                self.assertEqual(restored_root.state(), "zoomed")
            finally:
                restored.close()
                restored_root.destroy()

    def test_offscreen_window_uses_a_safe_primary_monitor_fallback(self) -> None:
        primary = MonitorInfo("primary", 0, 0, 1920, 1080, 96, 96, True)
        saved = WindowPlacement(900, 760, 5000, 5000, "normal")

        restored = restore_window_placement(saved, [primary])

        self.assertEqual(restored, WindowPlacement(900, 760, 510, 160, "normal"))

    def test_large_window_on_a_large_secondary_display_is_not_clamped_to_primary(self) -> None:
        primary = MonitorInfo("primary", 0, 0, 1920, 1080, 96, 96, True)
        secondary = MonitorInfo("large", 1920, 0, 5760, 2160, 144, 144, False)
        saved = WindowPlacement(2200, 1400, 2200, 120, "normal")

        restored = restore_window_placement(saved, [primary, secondary])

        self.assertEqual(restored, saved)


class CaptureWorkerTests(unittest.TestCase):
    def test_rapid_requests_run_one_ocr_at_a_time_and_only_publish_the_newest(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        published: list[str] = []
        recognised: list[str] = []
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def recognise(image: str) -> str:
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                recognised.append(image)
                if image == "first":
                    first_started.set()
                    release_first.wait(2)
                return image
            finally:
                with guard:
                    active -= 1

        images = {1: "first", 2: "discarded", 3: "newest"}
        worker = CaptureWorker(
            capture=lambda box: images[box[0]],
            recognise=recognise,
            correct=lambda raw, _settings: raw.upper(),
            on_result=lambda _job, result, _timings: published.append(result),
            on_error=lambda _job, error: self.fail(f"Unexpected worker error: {error}"),
        )
        try:
            worker.submit([1, 0, 11, 10], "first", {})
            self.assertTrue(first_started.wait(1))
            worker.submit([2, 0, 12, 10], "discarded", {})
            worker.submit([3, 0, 13, 10], "newest", {})
            release_first.set()
            self.assertTrue(worker.wait_until_idle(2))
        finally:
            release_first.set()
            worker.close()

        self.assertEqual(maximum_active, 1)
        self.assertEqual(recognised, ["first", "newest"])
        self.assertEqual(published, ["NEWEST"])


class ReaderStateTests(unittest.TestCase):
    def test_raw_corrected_successful_and_current_speech_are_distinct(self) -> None:
        state = ReaderTextState()
        result = CorrectionResult("In o second.", "In a second.", (), 0.2)

        state.accept_success(result)
        state.begin_speech("In a second.")

        self.assertEqual(state.raw_ocr_text, "In o second.")
        self.assertEqual(state.corrected_ocr_text, "In a second.")
        self.assertEqual(state.last_successful_text, "In a second.")
        self.assertEqual(state.currently_spoken_text, "In a second.")
        state.end_speech("In a second.")
        self.assertEqual(state.currently_spoken_text, "")

    def test_manual_text_becomes_replay_text_and_invalidates_ocr_details(self) -> None:
        state = ReaderTextState()
        state.accept_success(CorrectionResult("Noytibos", "Naytibas", (), 0.1))

        self.assertTrue(state.accept_manual_text("  Noytibos typed exactly  "))
        self.assertEqual(state.last_successful_text, "  Noytibos typed exactly  ")
        self.assertEqual(state.raw_ocr_text, "")
        self.assertEqual(state.corrected_ocr_text, "")
        self.assertTrue(state.can_read_again)

        self.assertFalse(state.accept_manual_text("  \n  "))
        self.assertFalse(state.can_read_again)

    def test_old_speech_completion_cannot_clear_newer_identical_text(self) -> None:
        state = ReaderTextState()
        state.begin_speech("Same line", request_id=1)
        state.begin_speech("Same line", request_id=2)
        state.end_speech("Same line", request_id=1)
        self.assertEqual(state.currently_spoken_text, "Same line")
        self.assertEqual(state.currently_spoken_request_id, 2)
        state.end_speech("Same line", request_id=2)
        self.assertEqual(state.currently_spoken_text, "")

    def test_read_again_uses_stored_final_text_without_capture_or_ocr(self) -> None:
        class Tts:
            def __init__(self) -> None:
                self.stopped = 0
                self.spoken: list[tuple[object, ...]] = []

            def stop(self) -> None:
                self.stopped += 1

            def speak(self, *args: object) -> threading.Event:
                self.spoken.append(args)
                return threading.Event()

        class Config:
            @staticmethod
            def get() -> dict[str, object]:
                return {"voice": "voice", "rate": 2, "volume": 88}

        class Ui:
            statuses: list[str] = []

            def set_status(self, message: str, error: bool = False) -> None:
                self.statuses.append(message)

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.tts = Tts()
        app.config = Config()
        app.ui = Ui()
        app.text_state = ReaderTextState(last_successful_text="Final corrected dialogue")
        app.capture_box = lambda *_args, **_kwargs: self.fail("Read Again took a screenshot")
        app.ocr = object()
        app.corrector = object()

        app.read_again()

        self.assertEqual(app.tts.stopped, 1)
        self.assertEqual(len(app.tts.spoken), 1)
        self.assertEqual(app.tts.spoken[0][0].spoken_text, "Final corrected dialogue.")
        self.assertEqual(app.tts.spoken[0][1:], ("voice", 2, 88))

    def test_read_again_speaks_manual_text_without_ocr_correction(self) -> None:
        spoken: list[tuple[object, ...]] = []

        class Tts:
            def stop(self) -> None:
                pass

            def speak(self, *args: object) -> threading.Event:
                spoken.append(args)
                return threading.Event()

        class Config:
            @staticmethod
            def get() -> dict[str, object]:
                return {"voice": "voice", "rate": 1, "volume": 70}

        class Ui:
            def set_status(self, _message: str, error: bool = False) -> None:
                pass

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.tts = Tts()
        app.config = Config()
        app.ui = Ui()
        app.text_state = ReaderTextState()
        app.text_state.accept_manual_text("Noytibos\nwrapped text")

        app.read_again()

        self.assertEqual(len(spoken), 1)
        self.assertEqual(spoken[0][0].spoken_text, "Noytibos wrapped text.")
        self.assertEqual(spoken[0][1:], ("voice", 1, 70))


class StartupFlowTests(unittest.TestCase):
    def test_configured_startup_repairs_the_current_registration(self) -> None:
        class Config:
            def __init__(self) -> None:
                self.data = {"startup": {"enabled": True}}

            def get(self) -> dict[str, object]:
                return self.data

            def update(self, **changes: object) -> dict[str, object]:
                self.data.update(changes)
                return self.data

        class Registration:
            calls: list[bool] = []

            def is_enabled(self) -> bool:
                return False

            def set_enabled(self, enabled: bool) -> None:
                self.calls.append(enabled)

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.config = Config()
        app.startup_registration = Registration()

        self.assertEqual(app._reconcile_startup_registration(app.config.get()), "")
        self.assertEqual(app.startup_registration.calls, [True])
        self.assertTrue(app.config.get()["startup"]["enabled"])

    def test_startup_registration_failure_rolls_back_the_preference(self) -> None:
        class Config:
            def __init__(self) -> None:
                self.data = {"startup": {"enabled": True}}

            def get(self) -> dict[str, object]:
                return self.data

            def update(self, **changes: object) -> dict[str, object]:
                self.data.update(changes)
                return self.data

        class Registration:
            def is_enabled(self) -> bool:
                raise StartupRegistrationError("blocked by policy")

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.config = Config()
        app.startup_registration = Registration()

        error = app._reconcile_startup_registration(app.config.get())
        self.assertEqual(error, "blocked by policy")
        self.assertFalse(app.config.get()["startup"]["enabled"])

    def test_hidden_startup_falls_back_to_visible_when_tray_fails(self) -> None:
        callbacks: list[object] = []
        statuses: list[tuple[str, bool]] = []

        class Root:
            deiconified = False

            def after_idle(self, callback: object) -> None:
                callbacks.append(callback)

            def deiconify(self) -> None:
                self.deiconified = True

        class Ui:
            def set_status(self, message: str, error: bool = False) -> None:
                statuses.append((message, error))

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.root = Root()
        app.ui = Ui()
        app.hide_window = lambda: callbacks.append("hidden")

        app._apply_startup_visibility(True, True)
        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        self.assertEqual(callbacks, ["hidden"])

        app._apply_startup_visibility(True, False)
        self.assertTrue(app.root.deiconified)
        self.assertTrue(statuses[-1][1])

    def test_startup_argument_only_hides_sign_in_launches(self) -> None:
        with (
            patch.object(main_module, "set_windows_app_identity"),
            patch.object(main_module, "enable_dpi_awareness"),
            patch.object(main_module, "GameTextReaderApplication") as application,
        ):
            main_module.main(["--startup"])
            application.assert_called_once_with(start_hidden=True)
            application.return_value.run.assert_called_once_with()

            application.reset_mock()
            main_module.main([])
            application.assert_called_once_with(start_hidden=False)


class ApplicationSpeechRoutingTests(unittest.TestCase):
    def _make_app(self, mode: str) -> tuple[GameTextReaderApplication, list[tuple[str, str, int, int]]]:
        calls: list[tuple[str, str, int, int]] = []

        class Config:
            @staticmethod
            def get() -> dict[str, object]:
                return {
                    "voice": "voice",
                    "rate": 2,
                    "volume": 88,
                    "speech": {"capture_mode": mode},
                }

        class Tts:
            def enqueue(self, text: str, voice: str, rate: int, volume: int) -> None:
                calls.append(("queue", text, rate, volume))

            def replace(self, text: str, voice: str, rate: int, volume: int) -> None:
                calls.append(("replace", text, rate, volume))

            def stop(self) -> None:
                raise AssertionError("a normal capture must not stop current speech")

        class Ui:
            def set_last_result(self, _result: object) -> None:
                pass

            def set_read_again_enabled(self, _enabled: bool) -> None:
                pass

            def set_status(self, _message: str, error: bool = False) -> None:
                pass

        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.config = Config()
        app.tts = Tts()
        app.ui = Ui()
        app.text_state = ReaderTextState()
        app._timing_lock = threading.Lock()
        app._pending_speech_timing = None
        app._schedule = lambda callback: callback()
        return app, calls

    def test_capture_result_queues_the_next_line_without_stopping_audio(self) -> None:
        app, calls = self._make_app("queue")
        job = CaptureJob(1, (0, 0, 10, 10), "Fixed box", app.config.get(), time.perf_counter())
        app._capture_succeeded(
            job,
            CorrectionResult("First", "First", (), 0.0),
            PipelineTimings(time.perf_counter(), time.perf_counter(), time.perf_counter(), time.perf_counter(), time.perf_counter()),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "queue")
        self.assertEqual(calls[0][1].spoken_text, "First.")
        self.assertEqual(calls[0][2:], (2, 88))

    def test_capture_result_can_replace_the_current_line_without_stopping_audio(self) -> None:
        app, calls = self._make_app("replace")
        job = CaptureJob(1, (0, 0, 10, 10), "Fixed box", app.config.get(), time.perf_counter())
        app._capture_succeeded(
            job,
            CorrectionResult("Next", "Next", (), 0.0),
            PipelineTimings(time.perf_counter(), time.perf_counter(), time.perf_counter(), time.perf_counter(), time.perf_counter()),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "replace")
        self.assertEqual(calls[0][1].spoken_text, "Next.")
        self.assertEqual(calls[0][2:], (2, 88))


class SpeechResourceTests(unittest.TestCase):
    def test_mapped_speech_emits_source_word_progress(self) -> None:
        progress: list[tuple[int, str, int, int]] = []
        started_documents: list[tuple[int, str]] = []

        class Session:
            def __init__(self) -> None:
                self.sent = False
                self.finished = False

            def prepare(self, _voice_id: str) -> None:
                pass

            def start(self, _request: object, on_started: object, replace: bool = False) -> None:
                on_started()  # type: ignore[operator]

            def drain_word_events(self) -> tuple[tuple[int, int], ...]:
                if self.sent:
                    return ()
                self.sent = True
                return ((7, 12),)

            def poll(self) -> bool:
                if self.sent:
                    self.finished = True
                return self.finished

            def stop(self) -> None:
                self.finished = True

            def close(self) -> None:
                pass

        document = prepare_for_speech("Title\n\nWorld")
        engine = TtsEngine(
            session_factory=Session,
            on_document_started_with_id=lambda request_id, source: started_documents.append(
                (request_id, source)
            ),
            on_word_with_id=lambda request_id, source, start, end: progress.append(
                (request_id, source, start, end)
            ),
        )
        try:
            ticket = engine.speak(document)
            self.assertTrue(ticket.wait(1))
        finally:
            engine.shutdown()

        self.assertEqual(
            progress,
            [(ticket.request_id, "Title\n\nWorld", 7, 12)],
        )
        self.assertEqual(
            started_documents,
            [(ticket.request_id, "Title\n\nWorld")],
        )

    def test_replacement_starts_without_waiting_for_backend_cleanup(self) -> None:
        started: list[str] = []
        first_started = threading.Event()
        second_started = threading.Event()

        class Session:
            def __init__(self) -> None:
                self.active: object | None = None
                self.replaced = False
                self.release_legacy = threading.Event()

            def prepare(self, _voice_id: str) -> None:
                pass

            def start(self, request: object, on_started: object, replace: bool = False) -> None:
                self.active = request
                self.replaced = self.replaced or replace
                on_started()  # type: ignore[operator]

            def play(self, request: object, on_started: object) -> None:
                # Compatibility path used by the current blocking engine. The
                # first request remains stuck in backend cleanup until stopped.
                self.start(request, on_started)
                self.release_legacy.wait(5)

            def poll(self) -> bool:
                # Simulate a backend that has audible tail/cleanup after the
                # first line. It never reports that line as finished on its own.
                return getattr(self.active, "text", "") == "Second"

            def stop(self) -> None:
                self.active = None
                self.release_legacy.set()

            def close(self) -> None:
                self.active = None
                self.release_legacy.set()

        def mark_started(text: str, _at: float) -> None:
            started.append(text)
            if text == "First":
                first_started.set()
            if text == "Second":
                second_started.set()

        session = Session()
        engine = TtsEngine(
            session_factory=lambda: session,
            initial_voice_id="sapi:test",
            on_started=mark_started,
        )
        try:
            first = engine.speak("First", "sapi:test")
            self.assertTrue(first_started.wait(1))
            second = engine.replace("Second", "sapi:test")
            self.assertTrue(second_started.wait(0.5), "replacement waited for backend cleanup")
            self.assertTrue(second.wait(0.5))
            self.assertTrue(first.wait(0.5))
            self.assertEqual(started, ["First", "Second"])
            self.assertTrue(session.replaced)
        finally:
            session.release_legacy.set()
            engine.shutdown()

    def test_queue_keeps_one_next_line_and_drops_older_pending_lines(self) -> None:
        started: list[str] = []
        first_started = threading.Event()
        third_started = threading.Event()

        class Session:
            def __init__(self) -> None:
                self.active: object | None = None
                self.finished = False

            def prepare(self, _voice_id: str) -> None:
                pass

            def start(self, request: object, on_started: object, replace: bool = False) -> None:
                self.active = request
                self.finished = False
                on_started()  # type: ignore[operator]

            def poll(self) -> bool:
                return self.finished

            def finish(self) -> None:
                self.finished = True

            def stop(self) -> None:
                self.active = None
                self.finished = True

            def close(self) -> None:
                self.stop()

        def mark_started(text: str, _at: float) -> None:
            started.append(text)
            if text == "First":
                first_started.set()
            if text == "Third":
                third_started.set()

        session = Session()
        engine = TtsEngine(session_factory=lambda: session, on_started=mark_started)
        try:
            first = engine.enqueue("First")
            self.assertTrue(first_started.wait(1))
            second = engine.enqueue("Second")
            third = engine.enqueue("Third")
            self.assertTrue(second.wait(0.5), "the replaced pending line stayed queued")
            self.assertFalse(third_started.wait(0.05))
            session.finish()
            self.assertTrue(third_started.wait(0.5))
            self.assertTrue(first.wait(0.5))
            self.assertEqual(started, ["First", "Third"])
        finally:
            engine.shutdown()

    def test_overlap_starts_two_sessions_and_reuses_the_oldest_slot(self) -> None:
        started: list[str] = []
        sessions: list[object] = []
        third_started = threading.Event()

        class Session:
            def __init__(self) -> None:
                self.request: object | None = None
                self.finished = False
                self.closed = False
                sessions.append(self)

            def prepare(self, _voice_id: str) -> None:
                pass

            def start(self, request: object, on_started: object, replace: bool = False) -> None:
                self.request = request
                self.finished = False
                on_started()  # type: ignore[operator]

            def poll(self) -> bool:
                return self.finished

            def stop(self) -> None:
                self.finished = True

            def close(self) -> None:
                self.closed = True

            def finish(self) -> None:
                self.finished = True

        def mark_started(text: str, _at: float) -> None:
            started.append(text)
            if text == "Third":
                third_started.set()

        engine = TtsEngine(
            session_factory=Session,
            initial_capture_mode="overlap",
            initial_max_overlap=2,
            on_started=mark_started,
        )
        try:
            first = engine.overlap("First")
            second = engine.overlap("Second")
            self.assertTrue(first.wait(1) is False or first.is_set())
            deadline = time.monotonic() + 1
            while len(sessions) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(sessions), 2)
            third = engine.overlap("Third")
            self.assertFalse(third.wait(0.05))
            sessions[0].finish()  # type: ignore[attr-defined]
            self.assertTrue(third_started.wait(1), "third overlap did not fill the freed slot")
            self.assertEqual(started[:3], ["First", "Second", "Third"])
            sessions[1].finish()  # type: ignore[attr-defined]
            sessions[2].finish()  # type: ignore[attr-defined]
            self.assertTrue(engine.wait_until_idle(1))
        finally:
            engine.shutdown()

    def test_one_backend_session_is_reused_and_reports_real_playback_boundaries(self) -> None:
        created = 0
        started: list[str] = []
        finished: list[str] = []

        class Session:
            def __init__(self) -> None:
                self.played: list[str] = []
                self.closed = 0

            def prepare(self, _voice_id: str) -> None:
                pass

            def play(self, request: object, on_started: object) -> None:
                text = getattr(request, "text")
                self.played.append(text)
                on_started()

            def close(self) -> None:
                self.closed += 1

        session = Session()

        def factory() -> Session:
            nonlocal created
            created += 1
            return session

        engine = TtsEngine(
            session_factory=factory,
            initial_voice_id="sapi:test",
            on_started=lambda text, _at: started.append(text),
            on_finished=finished.append,
        )
        try:
            self.assertTrue(engine.speak("First", "sapi:test").wait(1))
            self.assertTrue(engine.speak("Second", "sapi:test").wait(1))
        finally:
            engine.shutdown()

        self.assertEqual(created, 1)
        self.assertEqual(session.played, ["First", "Second"])
        self.assertEqual(session.closed, 1)
        self.assertEqual(started, ["First", "Second"])
        self.assertEqual(finished, ["First", "Second"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
