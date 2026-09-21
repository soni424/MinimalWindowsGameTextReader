"""Reader feature regressions, with silent speech sessions and isolated settings."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tkinter as tk

from config import ConfigStore
from ocr_correction import OcrCorrector, CorrectionOptions, ReplacementRule
from settings_ui import SettingsUI, _ShortcutRecorderDialog
from speech_text import prepare_for_speech, native_offset_map
from tts_engine import TtsEngine, _WindowsSpeechSession, _WordTiming
from datetime import timedelta
from hotkey_manager import HotkeyManager, HotkeyError
from main import GameTextReaderApplication


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


class ContextualPassageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corrector = OcrCorrector()
        cls.corrector.warm_up()

    def test_all_twelve_supplied_passages(self):
        cases = json.loads(Path(__file__).with_name("test_ocr_passages.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(example=case["number"]):
                result = self.corrector.correct(case["raw"])
                self.assertEqual(result.raw_text, case["raw"])
                if case["number"] not in (6, 12):
                    self.assertEqual(result.corrected_text, case["expected"])
                    self.assertFalse(result.suggestions)
                elif case["number"] == 6:
                    self.assertIn("a cove", result.corrected_text)
                    self.assertIn("Noytibos", result.corrected_text)  # unfamiliar game term, not guessed
                    self.assertIn("Vines, grapes, and a clear pond.", result.corrected_text)
                    self.assertTrue(result.corrected_text.endswith("painful reality"))
                    self.assertEqual([(s.original, s.alternatives) for s in result.suggestions],
                                     [("cove", ("cave",)), ("t\nt ace", ("that place",))])
                else:
                    self.assertIn("I'll have to camp", result.corrected_text)
                    self.assertIn("find an Angel", result.corrected_text)
                    self.assertEqual([(s.original, s.alternatives) for s in result.suggestions],
                                     [("ports", ("parts",)), ("Ing", ("winning",))])

    def test_negative_and_protected_dialogue_all_strengths(self):
        passage = ("Enjoy bock beer. I foiled their plan. She scored 42. "
                   "Explore the coves. The machine ports work. Travel to Xion. "
                   "Yohan, please stay. NaYtIbA-42 is strange. Thank you, Mory.")
        for strength in ("conservative", "balanced", "strong"):
            result = self.corrector.correct(passage, CorrectionOptions(strength=strength))
            self.assertEqual(result.corrected_text, passage)
        result = self.corrector.correct("Let's go bock.", CorrectionOptions(protected_words=("bock",)))
        self.assertEqual(result.corrected_text, "Let's go bock.")

    def test_custom_rules_apply_when_automatic_is_off_and_outputs_are_protected(self):
        options = CorrectionOptions(enabled=False, replacements=(ReplacementRule("Noytibos", "Naytibas"),))
        result = self.corrector.correct("Noytibos go bock.", options)
        self.assertEqual(result.corrected_text, "Naytibas go bock.")
        self.assertEqual(result.corrections[0].source, "custom")
        self.assertFalse(result.suggestions)
        options = CorrectionOptions(replacements=(ReplacementRule("beast", "go bock"),))
        self.assertEqual(self.corrector.correct("beast", options).corrected_text, "go bock")

    def test_review_updates_one_occurrence_rebases_remaining_and_preserves_raw(self):
        raw = "A pond in o cove. A pond in o cove."
        result = self.corrector.correct(raw)
        one, two = result.suggestions
        reviewed = result.review(one, "cave")
        self.assertEqual(reviewed.raw_text, raw)
        self.assertEqual(reviewed.corrected_text, "A pond in a cave. A pond in a cove.")
        self.assertEqual(reviewed.corrections[-1].source, "review")
        self.assertEqual(raw[reviewed.corrections[-1].start:reviewed.corrections[-1].end], "cove")
        with self.assertRaises(ValueError):
            reviewed.review(one, "cave")
        self.assertFalse(reviewed.review(reviewed.suggestions[0], None).suggestions)

    def test_review_longer_fragment_rebases_later_suggestion(self):
        result = self.corrector.correct("I wish to see t\nt ace. Machine ports.")
        first, second = result.suggestions
        reviewed = result.review(first, "that place")
        remaining = reviewed.suggestions[0]
        self.assertEqual(reviewed.corrected_text[remaining.start:remaining.end], "ports")
        self.assertEqual(remaining.start - second.start, len("that place") - len("t\nt ace"))

    def test_name_evidence_does_not_override_another_explicit_name(self):
        text = "Mary, do you know? Mory, please stay. Thank you...Mory."
        self.assertEqual(self.corrector.correct(text).corrected_text, text)

    def test_warmed_500_words_is_bounded(self):
        cases = json.loads(Path(__file__).with_name("test_ocr_passages.json").read_text(encoding="utf-8"))
        passage = " ".join(" ".join(p["raw"] for p in cases).split()[:500])
        for strength in ("conservative", "balanced", "strong"):
            durations = [self.corrector.correct(passage, CorrectionOptions(strength=strength)).elapsed_ms for _ in range(3)]
            self.assertLess(min(durations), 100, (strength, durations))


class SilentSession:
    def __init__(self):
        self.requests = []
        self.seeks = []
        self.events = []
        self.finished = False
        self.closed = False
        self.can_seek = True
    def prepare(self, _voice): pass
    def start(self, request, started, replace=False):
        self.requests.append(request)
        self.finished = False
        started()
    def poll(self): return self.finished
    def seek(self, offset):
        self.seeks.append(offset)
        return self.can_seek
    def drain_word_events(self):
        events, self.events = self.events, []
        return events
    def stop(self): self.finished = True
    def close(self): self.closed = True


class SeekTests(unittest.TestCase):
    def setUp(self):
        self.sessions, self.started, self.words, self.errors = [], [], [], []
        def factory():
            session = SilentSession()
            self.sessions.append(session)
            return session
        self.engine = TtsEngine(session_factory=factory,
            on_document_started_with_id=lambda *args: self.started.append(args),
            on_word_with_id=lambda *args: self.words.append(args), on_error=self.errors.append)
        self.doc = prepare_for_speech("1. 1 survivor's tale\n\n😀 Go back. Go forward.")
    def tearDown(self):
        self.engine.shutdown()
        self.assertFalse(self.errors)
    def start(self, overlap=False):
        ticket = (self.engine.overlap if overlap else self.engine.speak)(self.doc, "voice-id", 3, 0)
        self.assertTrue(eventually(lambda: any(i == ticket.request_id for i, _ in self.started)))
        return ticket
    def test_forward_backward_seek_reuses_stream_and_preserves_settings(self):
        ticket = self.start()
        for word in (self.doc.words[-1], self.doc.words[0]):
            revision = self.engine.seek_to_source(ticket.request_id, self.doc.source_text, word.source_start)
            self.assertIsNotNone(revision)
            self.assertTrue(eventually(lambda: self.started[-1][0] == revision.request_id))
            self.assertEqual(self.sessions[0].seeks[-1], len(self.doc.spoken_text[:word.spoken_start].encode("utf-16-le")) // 2)
            self.assertEqual(len(self.sessions[0].requests), 1)
            self.assertEqual(self.engine._current_request.volume, 0)
            self.assertEqual(self.engine._current_request.rate, 3)
        self.assertIsNone(self.engine.seek_to_source(revision.request_id, "changed text", 0))
    def test_fallback_synthesizes_suffix_and_can_return_to_earlier_word(self):
        ticket = self.start()
        self.sessions[0].can_seek = False
        offset = self.doc.words[-1].source_start
        revision = self.engine.seek_to_source(ticket.request_id, self.doc.source_text, offset)
        self.assertTrue(eventually(lambda: len(self.sessions[0].requests) == 2))
        self.assertEqual(self.sessions[0].requests[-1].text, "forward.")
        self.assertEqual(self.sessions[0].requests[-1].source_text, self.doc.source_text)
        back = self.engine.seek_to_source(revision.request_id, self.doc.source_text, 3)
        self.assertTrue(eventually(lambda: len(self.sessions[0].requests) == 3))
        self.assertTrue(self.sessions[0].requests[-1].text.startswith("1 survivor's"))
        self.assertEqual(self.sessions[0].requests[-1].word_spans[0].source_start, 3)
    def test_native_utf16_progress_maps_to_python_source(self):
        ticket = self.start()
        word = self.doc.words[-1]
        start = len(self.doc.spoken_text[:word.spoken_start].encode("utf-16-le")) // 2
        end = len(self.doc.spoken_text[:word.spoken_end].encode("utf-16-le")) // 2
        self.sessions[0].events.append((start, end))
        self.assertTrue(eventually(lambda: self.words))
        self.assertEqual(self.words[-1], (ticket.request_id, self.doc.source_text, word.source_start, word.source_end))
    def test_seek_cancels_overlap_and_pending_without_changing_saved_mode(self):
        self.engine.set_capture_mode("overlap", 2)
        first = self.start(overlap=True)
        second = self.start(overlap=True)
        pending = self.engine.overlap("waiting")
        target = self.doc.words[-1].source_start
        revision = self.engine.seek_to_source(second.request_id, self.doc.source_text, target)
        self.assertTrue(eventually(lambda: self.started[-1][0] == revision.request_id))
        self.assertTrue(first.is_set())
        self.assertTrue(pending.wait(1))
        self.assertEqual(self.engine.capture_mode, "overlap")
        self.assertEqual(list(self.engine._active_requests), [revision.request_id])
        self.assertTrue(self.sessions[0].closed)
    def test_rapid_seeks_latest_target_wins_and_stop_rejects_future_seek(self):
        ticket = self.start()
        with self.engine._current_lock:
            revisions = [self.engine.seek_to_source(ticket.request_id, self.doc.source_text, w.source_start)
                         for w in self.doc.words * 4]
        final = revisions[-1]
        self.assertTrue(eventually(lambda: self.started[-1][0] == final.request_id))
        self.assertTrue(all(r.wait(.5) for r in revisions[:-1]))
        self.engine.stop()
        self.assertTrue(final.wait(1))
        self.assertIsNone(self.engine.seek_to_source(final.request_id, self.doc.source_text, 3))


class ReaderUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.store = ConfigStore(Path(self.temp.name) / "config.json")
        self.store.load()
        self.tts = SimpleNamespace(list_voices=lambda: [], stop=lambda: None)
        self.reviewed = []
        self.ui = SettingsUI(self.root, self.store, self.tts, lambda: None, lambda: None,
                             lambda: None, lambda *args: None, on_review_result=self.reviewed.append)
    def tearDown(self):
        self.ui.close()
        self.root.destroy()
        self.temp.cleanup()
    def select(self, text, start, end):
        self.ui.set_last_text(text)
        self.ui.captured_text.tag_add("sel", f"1.0 + {start} chars", f"1.0 + {end} chars")
    def test_auto_read_speed_selector_saves_and_rolls_back_on_failure(self):
        def save_speed(speed):
            return self.store.update(auto_read={"speed": speed})["auto_read"]["speed"]
        self.ui.on_auto_read_speed_changed = save_speed
        self.ui.auto_read_speed.set("Fast")
        self.ui._auto_read_speed_changed()
        self.assertEqual(self.store.get()["auto_read"]["speed"], "fast")
        self.assertEqual(self.ui.auto_read_speed.get(), "Fast")
        self.ui.on_auto_read_speed_changed = lambda _speed: (_ for _ in ()).throw(PermissionError("locked"))
        self.ui.auto_read_speed.set("Normal")
        self.ui._auto_read_speed_changed()
        self.assertEqual(self.ui.auto_read_speed.get(), "Fast")
        self.assertIn("could not be saved", self.ui.status_value.get())
    def test_context_rule_workflows_cancel_and_duplicate_editing(self):
        text = "bock bock"
        self.select(text, 5, 9)
        rule = dict(original="bock", replacement="back", enabled=True, case_sensitive=False, whole_word=True)
        with patch("settings_ui._ReplacementDialog", return_value=SimpleNamespace(result=rule)) as dialog:
            self.ui._replacement_from_selection(False)
            self.assertEqual(self.ui.captured_text.get("1.0", "end-1c"), text)
            self.assertTrue(dialog.call_args.kwargs["focus_replacement"])
        self.select(text, 5, 9)
        with patch("settings_ui._ReplacementDialog", return_value=SimpleNamespace(result=rule)):
            self.ui._replacement_from_selection(True)
        self.assertEqual(self.ui.captured_text.get("1.0", "end-1c"), "bock back")
        self.assertEqual(len(self.ui._replacement_rules), 1)
        self.select("cancel", 0, 6)
        with patch("settings_ui._ReplacementDialog", return_value=SimpleNamespace(result=None)):
            self.ui._replacement_from_selection(True)
        self.assertEqual(self.ui.captured_text.get("1.0", "end-1c"), "cancel")
        self.assertEqual(len(self.ui._replacement_rules), 1)
    def test_review_rejects_stale_capture_and_manual_edit(self):
        result = OcrCorrector().correct("A pond in o cove.")
        self.ui.set_last_result(result)
        self.assertTrue(self.ui._review_suggestion(result, result.suggestions[0], "cave"))
        self.assertEqual(self.reviewed[-1].corrected_text, "A pond in a cave.")
        self.assertFalse(self.ui._review_suggestion(result, result.suggestions[0], "cave"))
        self.ui.set_last_result(result)
        self.ui.captured_text.insert("end", " manually edited")
        self.ui._captured_text_modified()
        self.assertFalse(self.ui._review_suggestion(result, result.suggestions[0], "cave"))
    def test_pending_shortcut_values_are_distinct_from_active(self):
        self.ui.set_hotkey_status(True, "Alt+Z", "Alt+S", "Alt+Q")
        self.ui.snippet_hotkey.set("Ctrl+Shift+T")
        self.assertIn("Unapplied", self.ui.shortcut_edit_status.get())
        self.assertIn("Alt+S", self.ui.hotkey_status.get())
        self.ui.set_hotkey_status(True, self.ui.fixed_hotkey.get(), "Ctrl+Shift+T", self.ui.read_again_hotkey.get())
        self.assertEqual(self.ui.shortcut_edit_status.get(), "These values are applied.")
    def test_unicode_highlight_follows_source_offsets(self):
        source = "😀 Go forward."
        self.ui.set_last_text(source)
        self.ui.show_speech_progress(1, source, 5, 12)
        self.assertEqual(self.ui.captured_text.get(*self.ui.captured_text.tag_ranges("speech_word")), "forward")
    def test_failed_settings_save_keeps_previous_values(self):
        before = self.store.get()
        with patch("config.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.update(hotkeys={"snippet": "Ctrl+Shift+T"})
        self.assertEqual(self.store.get(), before)

    def test_failed_apply_restores_native_registration_and_saved_values(self):
        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.config, app.ui = self.store, self.ui
        app.hotkeys = HotkeyManager(lambda: None, lambda: None)
        try:
            app.apply_hotkeys("", "Ctrl+Alt+F9", "")
            with patch("config.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    app.apply_hotkeys("", "Ctrl+Alt+F10", "")
            self.assertEqual(app.hotkeys.active_keys, ("", "Ctrl+Alt+F9", ""))
            self.assertEqual(self.store.get()["hotkeys"]["snippet"], "Ctrl+Alt+F9")
            self.assertIn("Ctrl+Alt+F9", self.ui.hotkey_status.get())
        finally:
            app.hotkeys.stop()

    def test_review_dismiss_does_not_interrupt_speech(self):
        result = OcrCorrector().correct("A pond in a cove.")
        self.ui.set_last_result(result)
        self.ui.begin_speech_progress(7, result.corrected_text)
        self.assertTrue(self.ui._review_suggestion(result, result.suggestions[0], None))
        self.assertEqual(self.ui._speech_highlight_owner, 7)
        self.assertFalse(self.reviewed)

    def test_context_menu_retains_selection_and_disables_absent_word(self):
        self.select("first second", 0, 5)
        with patch("tkinter.Menu.tk_popup"), patch("tkinter.Menu.grab_release"):
            self.ui._reader_context_menu(SimpleNamespace(num=3, x=5000, y=5000, x_root=1, y_root=1))
            self.assertEqual(self.ui.captured_text.get(*self.ui.captured_text.tag_ranges("sel")), "first")
            self.assertEqual(self.ui._reader_menu.entrycget(5, "state"), "normal")
            self.ui.captured_text.tag_remove("sel", "1.0", "end")
            self.ui._reader_context_menu(SimpleNamespace(num=3, x=5000, y=5000, x_root=1, y_root=1))
            self.assertEqual(self.ui._reader_menu.entrycget(5, "state"), "disabled")


class ModifierTests(unittest.TestCase):
    def test_left_right_modifiers_and_focus_reset(self):
        recorder = _ShortcutRecorderDialog.__new__(_ShortcutRecorderDialog)
        recorder._pressed_modifiers = set()
        recorder.prompt = SimpleNamespace(set=lambda value: None)
        recorder.window = SimpleNamespace(destroy=lambda: None)
        for key in ("Control_L", "Control_R", "Super_L"):
            recorder._key_pressed(SimpleNamespace(keysym=key, state=0))
        recorder._key_released(SimpleNamespace(keysym="Control_L", state=0))
        self.assertEqual(recorder._pressed_modifiers, {"Ctrl", "Win"})
        recorder._focus_lost()
        self.assertFalse(recorder._pressed_modifiers)
        recorder._key_pressed(SimpleNamespace(keysym="F8", state=0))
        self.assertEqual(recorder.result, "F8")


class NativeSeekTests(unittest.TestCase):
    def test_repeated_voice_discovery_keeps_winrt_runtime_alive(self):
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer
        for _ in range(3):
            self.assertTrue(TtsEngine.list_voices())
            synthesizer = SpeechSynthesizer()
            try:
                synthesizer.voice = SpeechSynthesizer.all_voices[0]
                self.assertTrue(synthesizer.voice.display_name)
            finally:
                synthesizer.close()

    def test_real_winrt_and_sapi_seek_muted_forward_and_backward(self):
        voices = TtsEngine.list_voices()
        for backend in ("winrt", "sapi"):
            voice = next((v for v in voices if v.engine == backend), None)
            self.assertIsNotNone(voice, f"No {backend} voice installed for verification")
            sessions, started, errors = [], [], []
            class CheckedSession(_WindowsSpeechSession):
                def __init__(self):
                    super().__init__()
                    self.seek_checks = []
                    self.seekable = False
                    sessions.append(self)
                def poll(self):
                    channel = self._winrt_current_channel
                    if channel and not self.seekable:
                        self.seekable = bool(channel.player.playback_session.can_seek)
                    return super().poll()
                def seek(self, offset):
                    channel = self._winrt_current_channel
                    result = super().seek(offset)
                    if result:
                        self.seek_checks.append((offset, channel is self._winrt_current_channel,
                            channel.player.playback_session.position.total_seconds()))
                    return result
            engine = TtsEngine(session_factory=CheckedSession,
                on_document_started_with_id=lambda *args: started.append(args),
                on_error=errors.append)
            source = "First we investigate the classroom. Next we follow the corridor to another room. Finally we return."
            document = prepare_for_speech(source)
            try:
                ticket = engine.speak(document, voice.identifier, 0, 0)
                self.assertTrue(eventually(lambda: started, 15), errors)
                self.assertTrue(eventually(lambda: sessions[0].seekable, 5))
                for target in (source.index("Finally"), 0):
                    revision = engine.seek_to_source(ticket.request_id, source, target)
                    self.assertIsNotNone(revision, (backend, target, list(engine._active_requests), errors))
                    self.assertTrue(eventually(lambda: started[-1][0] == revision.request_id, 10), errors)
                self.assertEqual(len(sessions[0].seek_checks), 2, f"{backend}: native seek unexpectedly fell back")
                self.assertTrue(all(check[1] for check in sessions[0].seek_checks))
                self.assertGreater(sessions[0].seek_checks[0][2], 1)
                self.assertLess(sessions[0].seek_checks[1][2], .5)
                self.assertFalse(errors)
            finally:
                engine.shutdown()


if __name__ == "__main__":
    unittest.main()
