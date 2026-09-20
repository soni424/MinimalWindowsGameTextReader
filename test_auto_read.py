"""No-audio regression checks for fixed-box Auto-Read."""

import time
import unittest
from types import SimpleNamespace

from PIL import Image

from auto_read import AutoReadWatcher, DialogueGate, ForegroundWindow
from config import validate_config
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
        self.assertEqual(validate_config({"hotkeys": {"fixed": "Alt+Z"}})["hotkeys"]["auto_read"], "")

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
