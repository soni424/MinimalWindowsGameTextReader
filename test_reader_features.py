"""Reader feature regressions, with silent speech sessions and isolated settings."""
import json
import io
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tkinter as tk

from config import ConfigStore
from ocr_correction import OcrCorrector, CorrectionOptions, ReplacementRule
from settings_ui import SettingsUI, _ShortcutRecorderDialog
from speech_text import SpeechPause, prepare_for_speech, native_offset_map
from tts_engine import TtsEngine, TtsError, _SpeechRequest, _WindowsSpeechSession, _WordTiming
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
        self.paused = False
        self.timing = True
        self.playback_rate = 1.0
        self.supports_rate = True
        self.poll_calls = 0
    def prepare(self, _voice): pass
    def start(self, request, started, replace=False):
        self.requests.append(request)
        self.finished = False
        self.paused = False
        self.playback_rate = request.playback_rate
        started()
    def poll(self):
        self.poll_calls += 1
        return self.finished and not self.paused
    def seek(self, offset, *, keep_paused=False):
        self.seeks.append((offset, keep_paused))
        self.paused = keep_paused
        return self.can_seek
    def pause(self):
        self.paused = True
        return True
    def resume(self):
        self.paused = False
        return True
    def has_word_timing(self): return self.timing
    def set_playback_rate(self, rate):
        if not self.supports_rate:
            return False
        self.playback_rate = rate
        return True
    def effective_playback_rate(self): return self.playback_rate
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
            self.assertEqual(self.sessions[0].seeks[-1][0], len(self.doc.spoken_text[:word.spoken_start].encode("utf-16-le")) // 2)
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


class ReaderTransportTests(unittest.TestCase):
    def test_sapi_reader_audio_cache_reuses_only_matching_complete_passages(self):
        from tts_engine import _ReaderAudioCache
        document = prepare_for_speech('Evidence supports this result.')
        cache = _ReaderAudioCache()
        synthesis_calls = []

        def make_request(text=None, voice='sapi:voice', rate=2, volume=60, pauses=()):
            return _SpeechRequest(1, text or document.spoken_text, voice, rate, volume,
                                  document=document, reader_controlled=True, pauses=pauses)

        def make_session():
            session = _WindowsSpeechSession()
            session._reader_audio_cache = cache

            def synth(request, *, structured=True):
                synthesis_calls.append((request.text, request.voice_id, request.rate,
                                        request.volume, request.pauses))
                request.native_offsets = (0, 1)
                request.spoken_to_native = (0, 1)
                session._sapi_word_timings = (_WordTiming(0.1, 0, 8),)
                return b'fake wav bytes'

            session._synthesise_sapi_wav = synth
            return session

        first = make_session()
        first_request = make_request()
        self.assertEqual(first._sapi_segment_audio(first_request)[0], b'fake wav bytes')
        second = make_session()
        repeated = make_request()
        self.assertEqual(second._sapi_segment_audio(repeated)[0], b'fake wav bytes')
        self.assertEqual(len(synthesis_calls), 1)
        self.assertEqual(repeated.native_offsets, first_request.native_offsets)
        self.assertEqual(second._sapi_word_timings, (_WordTiming(0.1, 0, 8),))
        for changed in (make_request(voice='sapi:other'), make_request(rate=3),
                        make_request(volume=50),
                        make_request(pauses=(SpeechPause(9, 9, 250),))):
            second._sapi_segment_audio(changed)
        self.assertEqual(len(synthesis_calls), 5)
        suffix = make_request(text='supports this result.')
        second._sapi_segment_audio(suffix)
        self.assertEqual(len(synthesis_calls), 6)

    def test_cancel_during_full_sapi_synthesis_never_starts_audio(self):
        document = prepare_for_speech('A longer passage that is no longer wanted.')
        request = _SpeechRequest(1, document.spoken_text, 'sapi:voice', 0, 50,
                                 document=document, reader_controlled=True)
        session = _WindowsSpeechSession()
        played = []

        def synth(piece):
            piece.cancel.set()
            return b'fake wav bytes', ()

        session._sapi_segment_audio = synth
        session._play_sapi_segment = lambda *args: played.append(args)
        session._start_sapi(request, lambda: None)
        self.assertEqual(played, [])

    def test_reader_audio_cache_does_not_store_oversized_audio(self):
        from tts_engine import _CachedSapiAudio, _ReaderAudioCache
        document = prepare_for_speech('A large speech buffer.')
        request = _SpeechRequest(1, document.spoken_text, 'sapi:voice', 0, 50,
                                 document=document, reader_controlled=True)
        cache = _ReaderAudioCache()
        with patch('tts_engine._MAX_READER_AUDIO_CACHE_BYTES', 4):
            cache.put(request, _CachedSapiAudio(b'12345', (), (), ()))
        self.assertIsNone(cache.get(request))

    def test_reader_rate_is_applied_before_playback_begins(self):
        from tts_engine import _PlaybackChannel
        events = []

        class Playback:
            position = timedelta(0)
            natural_duration = timedelta(seconds=3)
            _rate = 1.0

            @property
            def playback_rate(self):
                return self._rate

            @playback_rate.setter
            def playback_rate(self, value):
                events.append(('rate', value))
                self._rate = value

        class Player:
            def __init__(self):
                self.playback_session = Playback()
                self.volume = 1.0

            def play(self):
                events.append(('play', self.playback_session.playback_rate))

        session = _WindowsSpeechSession()
        session._ensure_winrt = lambda: None
        session._set_media_stream = lambda channel, stream: None
        session._new_playback_channel = lambda: _PlaybackChannel(Player())
        session._start_stream(object(), lambda: None, False, 3.0, playback_rate=0.9)
        self.assertEqual(events[:2], [('rate', 0.9), ('play', 0.9)])
        self.assertEqual(session.effective_playback_rate(), 0.9)

    def test_reader_remains_muted_until_rate_becomes_available_or_falls_back(self):
        from tts_engine import _PlaybackChannel

        class Playback:
            position = timedelta(0)
            natural_duration = timedelta(seconds=3)
            ready = False
            _rate = 1.0

            @property
            def playback_rate(self):
                return self._rate

            @playback_rate.setter
            def playback_rate(self, value):
                if not self.ready:
                    raise OSError('Rate not ready')
                self._rate = value

        class Player:
            def __init__(self):
                self.playback_session = Playback()
                self.volume = 1.0
                self.play_count = 0

            def play(self):
                self.play_count += 1

        def session_with_player():
            session = _WindowsSpeechSession()
            player = Player()
            session._ensure_winrt = lambda: None
            session._set_media_stream = lambda channel, stream: None
            session._new_playback_channel = lambda: _PlaybackChannel(player)
            return session, player

        session, player = session_with_player()
        started = []
        session._start_stream(object(), lambda: started.append(True), False, 3.0,
                              playback_rate=0.9)
        self.assertEqual(player.play_count, 1)
        self.assertEqual(player.volume, 0.0)
        self.assertFalse(started)
        player.playback_session.ready = True
        self.assertFalse(session.poll())
        self.assertEqual(player.volume, 1.0)
        self.assertEqual(started, [True])
        self.assertEqual(session.effective_playback_rate(), 0.9)

        session, player = session_with_player()
        started = []
        session._start_stream(object(), lambda: started.append(True), False, 3.0,
                              playback_rate=1.2)
        session._pending_rate_since = time.monotonic() - 2
        self.assertFalse(session.poll())
        self.assertEqual(player.volume, 1.0)
        self.assertEqual(started, [True])
        self.assertEqual(session.effective_playback_rate(), 1.0)
        self.assertTrue(session._playback_rate_error)

    def test_long_sapi_reader_uses_one_continuous_stream(self):
        document = prepare_for_speech(' '.join(f'Sentence {i} has several words.' for i in range(30)))
        request = _SpeechRequest(1, document.spoken_text, 'sapi:voice', 0, 50,
                                 source_text=document.source_text, word_spans=document.words,
                                 pauses=document.pauses, document=document, reader_controlled=True)
        session = _WindowsSpeechSession()
        played = []
        session._sapi_segment_audio = lambda piece: (b'wav', ())
        session._play_sapi_segment = lambda owner, piece, audio, started, replace=False: played.append(piece)
        session._start_sapi(request, lambda: None)
        self.assertEqual(len(played), 1)
        self.assertEqual(played[0].text, document.spoken_text)
        self.assertEqual(played[0].word_spans, document.words)

    def setUp(self):
        self.sessions = []
        self.states = []
        self.rates = []
        def factory():
            session = SilentSession()
            self.sessions.append(session)
            return session
        self.engine = TtsEngine(session_factory=factory,
            on_playback_state_with_id=lambda *state: self.states.append(state),
            on_reader_rate_result=lambda *result: self.rates.append(result))
        self.document = prepare_for_speech("First sentence. Second sentence! Third sentence?")

    def tearDown(self):
        self.engine.shutdown()

    def test_reader_pauses_and_seeks_without_interrupting_other_voices(self):
        other = self.engine.enqueue("An unrelated voice")
        self.assertTrue(eventually(lambda: other.request_id in self.engine._active_requests))
        queued = self.engine.enqueue("A queued voice")
        self.assertTrue(eventually(lambda: self.engine._pending_request is not None))
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        self.assertEqual(len(self.sessions), 2)
        self.assertTrue(self.engine.pause_request(reader.request_id, self.document.source_text))
        self.assertTrue(eventually(lambda: self.sessions[1].paused))
        self.assertFalse(self.sessions[0].paused)
        second = self.document.sentences[1].source_start
        revision = self.engine.navigate_reader(reader.request_id, self.document.source_text, second)
        self.assertIsNotNone(revision)
        self.assertTrue(eventually(lambda: revision.request_id in self.engine._active_requests))
        self.assertTrue(self.sessions[1].seeks[-1][1])
        self.assertTrue(self.sessions[1].paused)
        self.assertIn(other.request_id, self.engine._active_requests)
        self.assertEqual(self.engine._pending_request.request_id, queued.request_id)
        self.assertTrue(self.engine.resume_request(revision.request_id, self.document.source_text))
        self.assertTrue(eventually(lambda: not self.sessions[1].paused))

    def test_paused_fallback_waits_to_synthesize_until_resume(self):
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        session = self.sessions[0]
        session.can_seek = False
        self.assertTrue(self.engine.pause_request(reader.request_id, self.document.source_text))
        self.assertTrue(eventually(lambda: session.paused))
        target = self.document.sentences[2].source_start
        revision = self.engine.navigate_reader(reader.request_id, self.document.source_text, target)
        self.assertTrue(eventually(lambda: revision.request_id in self.engine._active_requests))
        self.assertEqual(len(session.requests), 1)
        self.assertTrue(self.engine.resume_request(revision.request_id, self.document.source_text))
        self.assertTrue(eventually(lambda: len(session.requests) == 2))
        self.assertEqual(session.requests[-1].text, "Third sentence?")

    def test_missing_timing_disables_sentence_navigation_but_allows_pause(self):
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        self.assertTrue(self.states[-1][3])
        self.sessions[0].timing = False
        self.engine._active_requests[reader.request_id].can_navigate = False
        self.assertIsNone(self.engine.navigate_reader(reader.request_id, self.document.source_text,
                                                      self.document.sentences[1].source_start))
        self.assertTrue(self.engine.pause_request(reader.request_id, self.document.source_text))

    def test_existing_double_click_seek_still_works_for_reader_playback(self):
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        target = self.document.words[-1].source_start
        revision = self.engine.seek_to_source(reader.request_id, self.document.source_text, target)
        self.assertIsNotNone(revision)
        self.assertTrue(eventually(lambda: revision.request_id in self.engine._active_requests))
        self.assertEqual(len(self.sessions[0].requests), 1)
        self.assertTrue(eventually(lambda: list(self.engine._active_requests) == [revision.request_id]))
        self.assertEqual(self.engine._current_request.request_id, revision.request_id)
        self.assertEqual(len(self.sessions), 1)
        # One native session must have one worker owner after seeking. The old
        # bug polled the same MediaPlayer from both active and reader_active.
        self.sessions[0].poll_calls = 0
        time.sleep(.22)
        self.assertLess(self.sessions[0].poll_calls, 18)

    def test_edit_cancels_new_seek_revision_even_with_previous_request_id(self):
        unrelated = self.engine.enqueue("Other voice")
        self.assertTrue(eventually(lambda: unrelated.request_id in self.engine._active_requests))
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        revision = self.engine.navigate_reader(reader.request_id, self.document.source_text,
                                               self.document.sentences[1].source_start)
        self.assertTrue(eventually(lambda: revision.request_id in self.engine._active_requests))
        self.assertTrue(self.engine.cancel_request(reader.request_id))
        self.assertTrue(revision.wait(1))
        self.assertIn(unrelated.request_id, self.engine._active_requests)

    def test_live_speed_targets_only_reader_and_survives_paused_seek(self):
        other = self.engine.overlap(self.document)
        self.assertTrue(eventually(lambda: other.request_id in self.engine._active_requests))
        reader = self.engine.play_reader(self.document, playback_rate=1.3)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        self.assertEqual(self.sessions[1].playback_rate, 1.3)
        self.assertFalse(self.engine.set_reader_playback_rate(other.request_id, self.document.source_text, 1.8))
        self.assertTrue(self.engine.pause_request(reader.request_id, self.document.source_text))
        self.assertTrue(eventually(lambda: self.sessions[1].paused))
        self.assertTrue(self.engine.set_reader_playback_rate(reader.request_id, self.document.source_text, 1.8))
        self.assertTrue(eventually(lambda: self.sessions[1].playback_rate == 1.8))
        self.assertTrue(self.sessions[1].paused)
        self.assertEqual(self.sessions[0].playback_rate, 1.0)
        next_sentence = self.document.sentences[1].source_start
        revision = self.engine.navigate_reader(reader.request_id, self.document.source_text, next_sentence)
        self.assertTrue(eventually(lambda: revision.request_id in self.engine._active_requests))
        self.assertEqual(self.engine._active_requests[revision.request_id].playback_rate, 1.8)
        self.assertTrue(self.engine.set_reader_playback_rate(reader.request_id, self.document.source_text, 0.6))
        self.assertTrue(eventually(lambda: self.sessions[1].playback_rate == 0.6))
        self.assertTrue(self.sessions[1].paused)

    def test_unsupported_speed_reports_last_working_rate(self):
        reader = self.engine.play_reader(self.document, playback_rate=1.2)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        self.sessions[0].supports_rate = False
        self.assertTrue(self.engine.set_reader_playback_rate(reader.request_id, self.document.source_text, 1.9))
        self.assertTrue(eventually(lambda: self.rates and self.rates[-1][2] == 1.9))
        self.assertEqual(self.rates[-1][3], 1.2)
        self.assertEqual(self.sessions[0].playback_rate, 1.2)

    def test_read_again_channel_accepts_targeted_speed_change(self):
        unrelated = self.engine.overlap("Other speech")
        self.assertTrue(eventually(lambda: unrelated.request_id in self.engine._active_requests))
        replay = self.engine.replace_reader(self.document, playback_rate=1.4)
        self.assertTrue(eventually(lambda: replay.request_id in self.engine._active_requests))
        self.assertTrue(self.engine.set_reader_playback_rate(replay.request_id,
                                                             self.document.source_text, 0.8))
        self.assertTrue(eventually(lambda: self.engine._active_requests[replay.request_id].playback_rate == 0.8))
        self.assertIn(unrelated.request_id, self.engine._active_requests)

    def test_stale_reader_speed_result_cannot_rollback_newer_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / "config.json")
            store.load()
            store.update(speech={"reader_playback_rate": 1.6})
            app = GameTextReaderApplication.__new__(GameTextReaderApplication)
            app.config = store
            app.text_state = SimpleNamespace(last_successful_text=self.document.source_text)
            app._reader_current_id = 8
            app._schedule = lambda callback: callback()
            messages = []
            app.ui = SimpleNamespace(set_reader_rate=lambda value: messages.append(value),
                                     set_status=lambda *_args, **_kwargs: None)
            app._reader_rate_result(7, self.document.source_text, 1.6, 1.0)
            self.assertEqual(store.get()["speech"]["reader_playback_rate"], 1.6)
            app._reader_rate_result(8, self.document.source_text, 1.6, 1.2)
            self.assertEqual(store.get()["speech"]["reader_playback_rate"], 1.2)
            self.assertEqual(messages, [1.2])

    def test_rapid_speed_changes_keep_latest_value(self):
        reader = self.engine.play_reader(self.document)
        self.assertTrue(eventually(lambda: reader.request_id in self.engine._active_requests))
        for value in (0.7, 1.4, 1.7):
            self.assertTrue(self.engine.set_reader_playback_rate(reader.request_id, self.document.source_text, value))
        self.assertTrue(eventually(lambda: self.sessions[0].playback_rate == 1.7))
        self.assertEqual(self.rates[-1][2:], (1.7, 1.7))

    def test_reader_entry_points_use_reader_rate_but_capture_voice_does_not(self):
        app = GameTextReaderApplication.__new__(GameTextReaderApplication)
        app.config = SimpleNamespace(get=lambda: {"voice": "voice-id", "rate": 3, "volume": 40,
            "speech": {"reader_playback_rate": 1.6}})
        app.text_state = SimpleNamespace(last_successful_text=self.document.source_text,
                                          end_speech=lambda: None)
        calls = []
        def record(kind, *args):
            calls.append((kind, *args))
            return SimpleNamespace(request_id=len(calls))
        app.tts = SimpleNamespace(stop=lambda: calls.append(("stop",)),
            replace_reader=lambda *args: record("again", *args),
            play_reader=lambda *args: record("play", *args))
        app.ui = SimpleNamespace(set_status=lambda *_args: None)
        app.read_again()
        app.reader_play_pause(None, self.document.source_text, False)
        self.assertEqual(calls[1][0], "again")
        self.assertEqual(calls[2][0], "play")
        self.assertEqual(calls[1][-1], 1.6)
        self.assertEqual(calls[2][-1], 1.6)
        self.assertEqual(calls[1][2:5], ("voice-id", 3, 40))


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
    def test_reader_transport_tracks_sentence_and_pause_state(self):
        source = "First wrapped\nline. Second sentence!"
        commands = []
        self.ui.on_reader_play_pause = lambda *args: commands.append(("play", *args))
        self.ui.on_reader_navigate = lambda *args: commands.append(("seek", *args))
        self.ui.set_last_text(source)
        self.ui._reader_play_pause()
        self.assertEqual(commands[-1], ("play", None, source, False))
        self.ui.begin_speech_progress(8, source)
        self.ui.set_reader_playback_state(8, source, False, True)
        self.assertEqual(self.ui.play_pause_button.cget("text"), "Pause")
        self.assertEqual(str(self.ui.next_sentence_button.cget("state")), "normal")
        self.ui._navigate_sentence(1)
        self.assertEqual(commands[-1], ("seek", 8, source, source.index("Second")))
        self.ui.show_speech_progress(8, source, source.index("Second"), source.index("Second") + 6)
        self.assertEqual(str(self.ui.previous_sentence_button.cget("state")), "normal")
        self.assertEqual(str(self.ui.next_sentence_button.cget("state")), "disabled")
        self.ui.set_reader_playback_state(8, source, True, True)
        self.ui._reader_play_pause()
        self.assertEqual(commands[-1], ("play", 8, source, True))
        self.ui.set_reader_playback_state(8, source, True, False)
        self.assertEqual(str(self.ui.previous_sentence_button.cget("state")), "disabled")
        self.assertIn("no word timing", self.ui.reader_transport_hint.get())
        self.ui.clear_speech_progress(8)
        self.assertEqual(self.ui.play_pause_button.cget("text"), "Play")
        self.ui._reflow_reader_actions(700)
        self.assertEqual(int(self.ui._reader_trailing_actions.grid_info()["row"]), 2)
        self.ui._reflow_reader_actions(1400)
        self.assertEqual(int(self.ui._reader_trailing_actions.grid_info()["row"]), 0)
    def test_reader_speed_control_saves_reset_and_wraps(self):
        changes = []
        self.ui.on_reader_speed_changed = lambda request_id, source, rate: (
            changes.append((request_id, source, rate)),
            self.store.update(speech={"reader_playback_rate": rate})["speech"]["reader_playback_rate"]
        )[1]
        self.ui.set_last_text("A visible passage.")
        self.ui.reader_rate_value.set(1.6)
        self.ui._reader_rate_moved("1.6")
        self.ui._save_reader_rate()
        self.assertEqual(changes[-1], (None, "A visible passage.", 1.6))
        self.assertEqual(self.store.get()["speech"]["reader_playback_rate"], 1.6)
        self.ui._reset_reader_rate()
        self.assertEqual(self.store.get()["speech"]["reader_playback_rate"], 1.0)
        self.ui._reflow_reader_actions(700)
        self.assertEqual(int(self.ui.reader_speed_controls.grid_info()["row"]), 1)
    def test_action_icons_keep_text_and_follow_theme(self):
        from action_icons import icon_for_button
        self.assertEqual(self.ui.play_pause_button.cget('text'), 'Play')
        self.assertTrue(self.ui.play_pause_button.cget('image'))
        old = self.ui.play_pause_button.cget('image')
        icon_for_button(self.ui.play_pause_button, not self.ui._palette.dark)
        self.assertNotEqual(self.ui.play_pause_button.cget('image'), old)
        self.assertEqual(str(self.ui.play_pause_button.cget('compound')), 'left')
    def test_auto_read_speed_selector_saves_and_rolls_back_on_failure(self):
        def save_speed(speed):
            return self.store.update(auto_read={"speed": speed})["auto_read"]["speed"]
        self.ui.on_auto_read_speed_changed = save_speed
        self.ui.auto_read_speed.set("Instant")
        self.ui._auto_read_speed_changed()
        self.assertEqual(self.store.get()["auto_read"]["speed"], "instant")
        self.assertEqual(self.ui.auto_read_speed.get(), "Instant")
        self.ui.on_auto_read_speed_changed = lambda _speed: (_ for _ in ()).throw(PermissionError("locked"))
        self.ui.auto_read_speed.set("Normal")
        self.ui._auto_read_speed_changed()
        self.assertEqual(self.ui.auto_read_speed.get(), "Instant")
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
    def test_native_sapi_reader_one_stream_finishes_one_request(self):
        voice = next((item for item in TtsEngine.list_voices() if item.engine == 'sapi'), None)
        if voice is None:
            self.skipTest('No SAPI voice is installed')
        source = ' '.join(
            f'Sentence {index} explains another observation in the classroom.'
            for index in range(9))
        sessions, started, finished, words, errors = [], [], [], [], []
        def factory():
            class CountedSession(_WindowsSpeechSession):
                def __init__(self):
                    super().__init__()
                    self.stream_starts = 0

                def _start_stream(self, *args, **kwargs):
                    self.stream_starts += 1
                    return super()._start_stream(*args, **kwargs)
            session = CountedSession()
            sessions.append(session)
            return session
        engine = TtsEngine(session_factory=factory,
            on_started_with_id=lambda *args: started.append(args),
            on_finished_with_id=lambda *args: finished.append(args),
            on_word_with_id=lambda *args: words.append(args), on_error=errors.append,
            initial_voice_id=voice.identifier)
        try:
            self.assertTrue(engine.wait_until_ready())
            document = prepare_for_speech(source)
            ticket = engine.play_reader(document, voice.identifier, 0, 0, 2.0)
            self.assertTrue(eventually(lambda: len(started) == 1, 8), errors)
            self.assertTrue(ticket.wait(25), errors)
            self.assertEqual(sessions[-1].stream_starts, 1)
            self.assertEqual(started[0][1], document.spoken_text)
            self.assertEqual(finished[-1][1], document.spoken_text)
            self.assertEqual({word[0] for word in words}, {ticket.request_id})
            self.assertFalse(errors)
        finally:
            engine.shutdown()

    def test_structured_synthesis_retries_plain_on_backend_rejection(self):
        document = prepare_for_speech('First line.\n\nSecond line.')
        for backend in ('winrt', 'sapi'):
            request = _SpeechRequest(1, document.spoken_text, '', 0, 0, pauses=document.pauses)
            session = _WindowsSpeechSession()
            session._start_stream = Mock()
            if backend == 'winrt':
                stream = object()
                session._winrt_loop = SimpleNamespace(run_until_complete=Mock(
                    side_effect=[ValueError('SSML unavailable'), stream]))
                session._winrt_synthesizer = SimpleNamespace(
                    options=SimpleNamespace(speaking_rate=1, audio_volume=1),
                    voice=SimpleNamespace(language='en-US'),
                    synthesize_ssml_to_stream_async=lambda value: value,
                    synthesize_text_to_stream_async=lambda value: value)
                with patch.object(session, '_winrt_word_timings', return_value=()):
                    session._start_winrt(request, lambda: None)
                self.assertEqual(session._winrt_loop.run_until_complete.call_count, 2)
            else:
                output = io.BytesIO()
                with wave.open(output, 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(22050)
                    wav.writeframes(b'\0' * 100)
                session._synthesise_sapi_wav = Mock(side_effect=[TtsError('XML unavailable'), output.getvalue()])
                session._bytes_to_winrt_stream = Mock(return_value=object())
                session._start_sapi(request, lambda: None)
                self.assertEqual(session._synthesise_sapi_wav.call_args_list[-1].kwargs,
                                 {'structured': False})
            if backend == 'winrt':
                self.assertEqual(request.native_offsets, native_offset_map(document.spoken_text))
            session._start_stream.assert_called_once()

    def test_structural_markup_keeps_real_voice_cues_and_seek_without_audio(self):
        from markdown_reader import prepare_markdown_for_speech
        source = ('### Science\n\nA & B are different from C.\n\n'
                  '- First topic explains the physical world.\n'
                  '  - Second topic explains living systems.\n'
                  '  - Third topic explains the atmosphere.\n\n---\n\n'
                  '### Principles\n\nEvidence is tested and revised over time.')
        document = prepare_markdown_for_speech(source)
        self.assertTrue(document.pauses)
        voices = TtsEngine.list_voices()
        for backend in ('winrt', 'sapi'):
            voice = next((item for item in voices if item.engine == backend), None)
            self.assertIsNotNone(voice)
            sessions, errors = [], []
            class CheckedSession(_WindowsSpeechSession):
                def __init__(self):
                    super().__init__()
                    sessions.append(self)
            engine = TtsEngine(session_factory=CheckedSession, on_error=errors.append)
            try:
                ticket = engine.play_reader(document, voice.identifier, 0, 0)
                self.assertTrue(eventually(lambda: ticket.request_id in engine._active_requests, 15), errors)
                request = engine._active_requests[ticket.request_id]
                self.assertEqual(request.pauses, document.pauses)
                self.assertTrue(eventually(lambda: bool(sessions[0]._winrt_current_channel and
                                                 sessions[0]._winrt_current_channel.word_timings), 15), errors)
                cues = sessions[0]._winrt_current_channel.word_timings
                mapped = [request.native_offsets[min(cue.spoken_start, len(request.native_offsets)-1)]
                          for cue in cues]
                self.assertTrue(any(document.spoken_text[offset:].startswith('Science') for offset in mapped))
                self.assertTrue(any(document.spoken_text[offset:].startswith('Principles') for offset in mapped))
                self.assertTrue(eventually(lambda: sessions[0]._winrt_current_channel.player.playback_session.can_seek, 3))
                revision = engine.seek_to_source(ticket.request_id, source, source.index('Principles'))
                self.assertIsNotNone(revision)
                self.assertTrue(eventually(lambda: revision.request_id in engine._active_requests, 5), errors)
                self.assertFalse(errors)
            finally:
                engine.shutdown()

    def test_native_reader_speed_changes_for_winrt_and_sapi_without_audio(self):
        voices = TtsEngine.list_voices()
        source = "First sentence remains visible. Second sentence also remains visible."
        for backend in ("winrt", "sapi"):
            voice = next((item for item in voices if item.engine == backend), None)
            self.assertIsNotNone(voice)
            sessions, rates, errors = [], [], []
            class CheckedSession(_WindowsSpeechSession):
                def __init__(self):
                    super().__init__()
                    sessions.append(self)
            engine = TtsEngine(session_factory=CheckedSession,
                on_reader_rate_result=lambda *result: rates.append(result), on_error=errors.append)
            try:
                ticket = engine.play_reader(prepare_for_speech(source), voice.identifier, 0, 0, 1.2)
                self.assertTrue(eventually(lambda: rates and rates[-1][0] == ticket.request_id, 15), errors)
                self.assertAlmostEqual(rates[-1][-1], 1.2, msg=sessions[0]._playback_rate_error)
                self.assertTrue(engine.pause_request(ticket.request_id, source))
                self.assertTrue(eventually(lambda: sessions[0]._paused_at > 0, 3))
                self.assertTrue(engine.set_reader_playback_rate(ticket.request_id, source, 0.7))
                self.assertTrue(eventually(lambda: rates[-1][2] == 0.7, 3), errors)
                self.assertAlmostEqual(rates[-1][-1], 0.7)
                self.assertTrue(sessions[0]._paused_at > 0)
                self.assertFalse(errors)
            finally:
                engine.shutdown()

    def test_native_reader_pause_resume_preserves_stream_and_watchdog(self):
        voices = TtsEngine.list_voices()
        source = ("First we investigate the classroom and look for clues. "
                  "Next we follow the corridor toward another room. "
                  "Finally we return to discuss the evidence we found.")
        for backend in ("winrt", "sapi"):
            voice = next((item for item in voices if item.engine == backend), None)
            self.assertIsNotNone(voice)
            sessions, states, errors = [], [], []
            class CheckedSession(_WindowsSpeechSession):
                def __init__(self):
                    super().__init__()
                    sessions.append(self)
            engine = TtsEngine(session_factory=CheckedSession,
                on_playback_state_with_id=lambda *state: states.append(state), on_error=errors.append)
            try:
                ticket = engine.play_reader(prepare_for_speech(source), voice.identifier, 0, 0)
                self.assertTrue(eventually(lambda: states and states[-1][0] == ticket.request_id, 15), errors)
                self.assertTrue(states[-1][3], f"{backend}: no native word timing")
                self.assertTrue(engine.pause_request(ticket.request_id, source))
                self.assertTrue(eventually(lambda: states[-1][2], 3), errors)
                channel = sessions[0]._winrt_current_channel
                def can_seek_after_media_initializes():
                    try:
                        return channel.player.playback_session.can_seek
                    except OSError:
                        return False
                self.assertTrue(eventually(can_seek_after_media_initializes, 3))
                target = prepare_for_speech(source).sentences[1].source_start
                revision = engine.navigate_reader(ticket.request_id, source, target)
                self.assertIsNotNone(revision)
                self.assertTrue(eventually(lambda: states[-1][0] == revision.request_id, 3), errors)
                self.assertTrue(states[-1][2])
                self.assertIs(channel, sessions[0]._winrt_current_channel)
                deadline = sessions[0]._winrt_deadline
                time.sleep(0.15)
                self.assertFalse(revision.is_set())
                self.assertTrue(engine.resume_request(revision.request_id, source))
                self.assertTrue(eventually(lambda: not states[-1][2], 3), errors)
                self.assertGreater(sessions[0]._winrt_deadline, deadline + 0.10)
                self.assertFalse(errors)
            finally:
                engine.shutdown()

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
