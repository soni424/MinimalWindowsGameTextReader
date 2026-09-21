"""No-audio regression checks for fixed-box Auto-Read."""

import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from auto_read import AUTO_READ_POLICIES, AutoReadWatcher, DialogueGate, ForegroundWindow
from config import ConfigStore, validate_config
from capture_pipeline import CaptureWorker
from capture_pipeline import CaptureJob, PipelineTimings
from main import GameTextReaderApplication
from hotkey_manager import HotkeyError, HotkeyManager
from ocr_correction import CorrectionResult
from reader_state import ReaderTextState


BOX = [100, 100, 400, 300]
GAME = ForegroundWindow(21, 42, "Example game", (0, 0, 1920, 1080))


class DialogueGateTests(unittest.TestCase):
    def test_stable_text_is_spoken_once_until_two_blank_results(self):
        gate = DialogueGate()
        self.assertFalse(gate.accept("Yukari: Hello"))
        self.assertTrue(gate.accept("Yukari:  Hello"))
        self.assertFalse(gate.accept("Yukari: Hello"))
        self.assertFalse(gate.accept(""))
        self.assertFalse(gate.accept(""))
        self.assertFalse(gate.accept("Yukari: Hello"))
        self.assertTrue(gate.accept("Yukari: Hello"))

    def test_typewriter_and_flicker_do_not_speak_transient_text(self):
        gate = DialogueGate()
        for fragment in ("I", "I saw", "I saw it", "I saw i", "I saw it"):
            self.assertFalse(gate.accept(fragment))
        self.assertTrue(gate.accept("I saw it"))
        self.assertFalse(gate.accept("I saw it"))

    def test_manual_read_takes_ownership_of_same_line(self):
        gate = DialogueGate()
        gate.remember_manual("Already read.")
        self.assertFalse(gate.accept("Already read."))
        self.assertFalse(gate.accept("A different line."))
        self.assertTrue(gate.accept("A different line."))

    def test_old_configuration_has_no_auto_shortcut(self):
        validated = validate_config({"hotkeys": {"fixed": "Alt+Z"}})
        self.assertEqual(validated["hotkeys"]["auto_read"], "")
        self.assertEqual(validated["auto_read"]["speed"], "normal")

    def test_speed_validation_and_persistence(self):
        self.assertEqual(validate_config({"auto_read": {"speed": "FAST"}})["auto_read"]["speed"], "fast")
        self.assertEqual(validate_config({"auto_read": {"speed": "INSTANT"}})["auto_read"]["speed"], "instant")
        self.assertEqual(validate_config({"auto_read": {"speed": "unsafe"}})["auto_read"]["speed"], "normal")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            store = ConfigStore(path)
            store.load()
            store.update(auto_read={"speed": "instant"})
            reloaded = ConfigStore(path)
            self.assertEqual(reloaded.load()["auto_read"]["speed"], "instant")

    def test_instant_first_result_noise_similarity_and_blank_rearming(self):
        policy = AUTO_READ_POLICIES["instant"]
        gate = DialogueGate()
        self.assertEqual(gate.observe("A complete line.", policy, now=0.0), "speak")
        self.assertEqual(gate.observe("A complete line!", policy, now=0.1), "ignore")
        self.assertEqual(gate.observe("A complete line!", policy, now=0.2), "speak")
        self.assertEqual(gate.observe("", policy, now=0.3), "ignore")
        self.assertEqual(gate.observe("", policy, now=0.4), "ignore")
        self.assertEqual(gate.observe("x", policy, now=0.5), "ignore")
        self.assertEqual(gate.observe("x", policy, now=0.6), "speak")

    def test_instant_typewriter_growth_cancels_once_then_restarts_when_stable(self):
        policy = AUTO_READ_POLICIES["instant"]
        gate = DialogueGate()
        self.assertEqual(gate.observe("I saw", policy, now=0.0), "speak")
        self.assertEqual(gate.observe("I saw i", policy, now=0.1), "cancel_growth")
        self.assertEqual(gate.observe("I saw it", policy, now=0.2), "ignore")
        self.assertEqual(gate.observe("I saw it", policy, now=0.3), "ignore")
        self.assertTrue(gate.needs_more_scans(policy, now=0.4))
        self.assertEqual(gate.observe("I saw it", policy, now=0.46), "speak")
        self.assertFalse(gate.recovering_growth)

    def test_auto_shortcut_uses_fourth_callback_and_cannot_duplicate_another_key(self):
        called = []
        manager = HotkeyManager(lambda: None, lambda: None,
                                on_auto_read=lambda: called.append("auto"))
        self.assertEqual(len(manager.active_keys), 4)
        manager._callbacks[3]()
        self.assertEqual(called, ["auto"])
        with self.assertRaises(HotkeyError):
            manager.apply_all("Alt+Z", "Alt+S", "", "Alt+S")


class WatcherTests(unittest.TestCase):
    def test_fast_policy_becomes_due_before_normal_without_skipping_confirmation(self):
        fast = AUTO_READ_POLICIES["fast"]
        normal = AUTO_READ_POLICIES["normal"]
        self.assertTrue(AutoReadWatcher._scan_due(0.20, 1, 0.0, 0.0, -1, 0, fast))
        self.assertFalse(AutoReadWatcher._scan_due(0.20, 1, 0.0, 0.0, -1, 0, normal))
        self.assertEqual(DialogueGate().confirmations, 0)
        gate = DialogueGate()
        self.assertFalse(gate.accept("A complete line."))
        self.assertTrue(gate.accept("A complete line."))

    def test_instant_policy_is_bounded_and_cancels_only_current_growth(self):
        instant = AUTO_READ_POLICIES["instant"]
        self.assertEqual(instant.confirmations, 1)
        self.assertLessEqual(instant.poll_interval, 0.06)
        cancelled = []
        watcher = AutoReadWatcher(
            lambda _: None, lambda *_: True, lambda *_: None,
            cancel_speech=lambda: cancelled.append(True), speed="instant",
        )
        watcher.active = True
        watcher.session = 3
        watcher.revision = 1
        self.assertTrue(watcher.accept_result(3, 1, "The first fragment"))
        self.assertFalse(watcher.accept_result(3, 1, "The first fragment grows"))
        self.assertEqual(cancelled, [True])
        self.assertFalse(watcher.accept_result(2, 1, "Stale growth must not cancel"))
        self.assertEqual(cancelled, [True])

    def test_switching_speed_keeps_active_game_and_dialogue_history(self):
        states = []
        watcher = AutoReadWatcher(lambda _: None, lambda *_: True, lambda *args: states.append(args))
        watcher.active = True
        watcher.profile = "Persona"
        watcher.target = GAME
        watcher.gate.remember_manual("Already spoken")
        watcher.set_speed("fast")
        self.assertEqual(watcher.speed, "fast")
        self.assertEqual(watcher.target, GAME)
        self.assertEqual(watcher.gate.last_spoken, "already spoken")
        self.assertIn("Fast mode", states[-1][1])
        watcher.gate.candidate = "transient"
        watcher.gate.confirmations = 1
        watcher.set_speed("instant")
        self.assertEqual(watcher.gate.candidate, "")
        self.assertEqual(watcher.gate.last_spoken, "already spoken")
        self.assertIn("Instant mode", states[-1][1])

    def test_arm_bind_pause_resume_and_stop_rejects_stale_ocr(self):
        foreground = [None]
        states = []
        submitted = []
        watcher = AutoReadWatcher(
            capture=lambda _box: Image.new("RGB", (20, 20), "white"),
            submit=lambda image, box, session, revision: submitted.append((session, revision)) or True,
            on_state=lambda active, message, error: states.append((active, message)),
            foreground=lambda: foreground[0],
            exists=lambda _target: True,
            interval=0.015,
        )
        try:
            watcher.start(BOX, "Default")
            self.assertTrue(watcher.active)
            self.assertIsNone(watcher.target)
            foreground[0] = GAME
            deadline = time.monotonic() + 2
            while watcher.target is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(watcher.target, GAME)
            self.assertFalse(watcher.accept_result(watcher.session, watcher.revision, "First"))
            self.assertTrue(watcher.accept_result(watcher.session, watcher.revision, "First"))
            foreground[0] = None
            deadline = time.monotonic() + 1
            while not any("paused" in message for _, message in states) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(any("paused" in message for _, message in states))
            foreground[0] = GAME
            deadline = time.monotonic() + 1
            while sum("Auto-reading" in message for _, message in states) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(sum("Auto-reading" in message for _, message in states), 2)
            old_session, old_revision = watcher.session, watcher.revision
            watcher.stop()
            self.assertFalse(watcher.accept_result(old_session, old_revision, "Stale"))
        finally:
            watcher.stop(notify=False)

    def test_three_errors_stop_without_blocking_ocr_callback(self):
        watcher = AutoReadWatcher(lambda _: None, lambda *_: True, lambda *_: None)
        watcher.active = True
        watcher.session = 7
        started = time.monotonic()
        for _ in range(3):
            watcher.note_error(7, 0, "OCR failed")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(watcher.active)

    def test_pending_ocr_prevents_additional_screenshots(self):
        foreground = [GAME]
        captures = []
        submitted = []
        watcher = AutoReadWatcher(
            capture=lambda _box: captures.append(time.monotonic()) or Image.new("RGB", (20, 20), "white"),
            submit=lambda *_args: submitted.append(True) or True,
            on_state=lambda *_args: None,
            foreground=lambda: foreground[0],
            exists=lambda _target: True,
            speed="fast",
            interval=0.01,
        )
        try:
            watcher.start(BOX, "Default")
            deadline = time.monotonic() + 1.5
            while not submitted and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(submitted)
            count = len(captures)
            time.sleep(0.08)
            self.assertEqual(len(captures), count)
        finally:
            watcher.stop(notify=False)


class PipelineTests(unittest.TestCase):
    def test_precaptured_auto_image_uses_shared_ocr_without_second_screenshot(self):
        import threading

        screenshot_calls = []
        finished = threading.Event()
        outputs = []
        worker = CaptureWorker(
            capture=lambda box: screenshot_calls.append(box),
            recognise=lambda image, _settings, _current: "Example dialogue" if image == "cached image" else "wrong",
            correct=lambda raw, _settings: raw.upper(),
            on_result=lambda job, result, _timing: (outputs.append((job, result)), finished.set()),
            on_error=lambda _job, error: self.fail(str(error)),
        )
        try:
            worker.submit(BOX, "Auto-Read", {}, image="cached image", auto_session=4, auto_revision=8)
            self.assertTrue(finished.wait(2))
            self.assertEqual(screenshot_calls, [])
            job, result = outputs[0]
            self.assertEqual(result, "EXAMPLE DIALOGUE")
            self.assertEqual((job.auto_session, job.auto_revision), (4, 8))
        finally:
            worker.close()


class ApplicationRoutingTests(unittest.TestCase):
    def test_auto_capture_confirms_then_replaces_even_when_manual_mode_is_queue(self):
        import threading

        spoken = []
        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.auto_read = AutoReadWatcher(lambda _: None, lambda *_: True, lambda *_: None)
        app.auto_read.active = True
        app.auto_read.session = 5
        app.auto_read.revision = 2
        app.config = SimpleNamespace(get=lambda: {})
        app.tts = SimpleNamespace(replace=lambda *args: spoken.append(args))
        app.ui = SimpleNamespace(set_last_result=lambda _: None,
                                 set_read_again_enabled=lambda _: None,
                                 set_status=lambda *_args, **_kwargs: None)
        app.text_state = ReaderTextState()
        app._timing_lock = threading.Lock()
        app._pending_speech_timing = None
        app._schedule = lambda callback: callback()
        settings = {"voice": "test", "rate": 1, "volume": 50,
                    "speech": {"capture_mode": "queue"}}
        job = CaptureJob(1, tuple(BOX), "Auto-Read", settings, time.perf_counter(),
                         auto_session=5, auto_revision=2)
        now = time.perf_counter()
        timings = PipelineTimings(now, now, now, now, now)
        result = CorrectionResult("Hello", "Hello", (), 0.0)
        app._capture_succeeded(job, result, timings)
        self.assertEqual(spoken, [])
        app._capture_succeeded(job, result, timings)
        self.assertEqual(len(spoken), 1)
        self.assertEqual(spoken[0][0].spoken_text, "Hello.")
        self.assertEqual(app.text_state.last_successful_text, "Hello")
        app.auto_read.stop(notify=False)
        app._capture_succeeded(job, CorrectionResult("Stale", "Stale", (), 0.0), timings)
        self.assertEqual(len(spoken), 1)


if __name__ == "__main__":
    unittest.main()
