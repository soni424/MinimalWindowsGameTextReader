"""Non-blocking native Windows speech with reusable audio resources."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class TtsError(RuntimeError):
    """Raised when no Windows speech backend can play requested speech."""


@dataclass(frozen=True)
class Voice:
    """A selectable installed Windows voice."""

    identifier: str
    display_name: str
    engine: str
    native_identifier: str


@dataclass
class _SpeechRequest:
    text: str
    voice_id: str
    rate: int
    volume: int
    generation: int = 0
    completion: threading.Event | None = None
    error: Exception | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)


class _SpeechSession(Protocol):
    def prepare(self, voice_id: str) -> None: ...

    def play(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None: ...

    def close(self) -> None: ...


class _WindowsSpeechSession:
    """Worker-thread-owned SAPI/WinRT resources reused between utterances."""

    def __init__(self) -> None:
        self._sapi_speaker = None
        self._sapi_voices: dict[str, object] = {}
        self._selected_sapi_voice = ""
        self._winrt_loop: asyncio.AbstractEventLoop | None = None
        self._winrt_synthesizer = None
        self._winrt_player = None
        self._winrt_media_source = None
        self._winrt_voice_type = None
        self._winrt_stream = None
        self._winrt_finished = threading.Event()
        self._selected_winrt_voice = ""

    def prepare(self, voice_id: str) -> None:
        if voice_id.startswith("winrt:"):
            self._ensure_winrt()
            self._select_winrt_voice(voice_id.removeprefix("winrt:"))
        else:
            self._ensure_sapi()
            self._select_sapi_voice(voice_id)

    def play(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None:
        if request.cancel.is_set():
            return
        if request.voice_id.startswith("winrt:"):
            playback_started = False

            def winrt_started() -> None:
                nonlocal playback_started
                playback_started = True
                on_started()

            try:
                self.prepare(request.voice_id)
                self._play_winrt(request, winrt_started)
                return
            except Exception:
                if playback_started:
                    raise
                # A OneCore voice may disappear between enumeration and use.
                # The persistent SAPI session still provides audible output.
                pass
        self.prepare(request.voice_id if request.voice_id.startswith("sapi:") else "")
        self._play_sapi(request, on_started)

    def _ensure_sapi(self) -> None:
        if self._sapi_speaker is not None:
            return
        try:
            import win32com.client

            speaker = win32com.client.Dispatch("SAPI.SpVoice")
        except ImportError as exc:
            raise TtsError("pywin32/SAPI5 is not available on this Windows installation.") from exc
        except Exception as exc:
            raise TtsError(f"Windows SAPI initialization failed: {exc}") from exc
        voices: dict[str, object] = {}
        try:
            collection = speaker.GetVoices()
            for index in range(collection.Count):
                token = collection.Item(index)
                voices[str(token.Id)] = token
        except Exception:
            voices = {}
        self._sapi_speaker = speaker
        self._sapi_voices = voices

    def _select_sapi_voice(self, voice_id: str) -> None:
        self._ensure_sapi()
        if not voice_id.startswith("sapi:"):
            return
        wanted = voice_id.removeprefix("sapi:")
        if wanted == self._selected_sapi_voice:
            return
        token = self._sapi_voices.get(wanted)
        if token is not None:
            self._sapi_speaker.Voice = token
            self._selected_sapi_voice = wanted

    def _play_sapi(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None:
        speaker = self._sapi_speaker
        try:
            speaker.Rate = request.rate
            speaker.Volume = request.volume
            speaker.Speak(request.text, 1)  # SVSFlagsAsync
            if request.cancel.is_set():
                speaker.Speak("", 2)  # Purge without creating an empty async stream.
                return
            on_started()
            while not speaker.WaitUntilDone(50):
                if request.cancel.is_set():
                    speaker.Speak("", 2)
                    break
        except Exception as exc:
            raise TtsError(f"Windows SAPI playback failed: {exc}") from exc

    def _ensure_winrt(self) -> None:
        if self._winrt_synthesizer is not None and self._winrt_player is not None:
            return
        from winrt.windows.media.core import MediaSource
        from winrt.windows.media.playback import MediaPlayer
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer

        self._winrt_loop = asyncio.new_event_loop()
        self._winrt_synthesizer = SpeechSynthesizer()
        self._winrt_player = MediaPlayer()
        self._winrt_media_source = MediaSource
        self._winrt_voice_type = SpeechSynthesizer
        self._winrt_player.media_ended += self._winrt_playback_ended
        self._winrt_player.media_failed += self._winrt_playback_ended

    def _winrt_playback_ended(self, *_args: object) -> None:
        self._winrt_finished.set()

    def _select_winrt_voice(self, native_id: str) -> None:
        if native_id == self._selected_winrt_voice:
            return
        selected = next(
            (voice for voice in self._winrt_voice_type.all_voices if str(voice.id) == native_id),
            None,
        )
        if selected is None:
            raise TtsError("The selected Windows voice is no longer installed.")
        self._winrt_synthesizer.voice = selected
        self._selected_winrt_voice = native_id

    def _play_winrt(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None:
        synthesizer = self._winrt_synthesizer
        synthesizer.options.speaking_rate = max(0.25, min(2.0, 1.0 + request.rate / 10.0))
        synthesizer.options.audio_volume = request.volume / 100.0
        stream = self._winrt_loop.run_until_complete(synthesizer.synthesize_text_to_stream_async(request.text))
        if request.cancel.is_set():
            try:
                stream.close()
            except Exception:
                pass
            return
        source = self._winrt_media_source.create_from_stream(stream, stream.content_type)
        previous_stream = self._winrt_stream
        self._winrt_stream = stream
        self._winrt_finished.clear()
        self._winrt_player.source = source
        if previous_stream is not None:
            try:
                previous_stream.close()
            except Exception:
                pass
        self._winrt_player.play()
        on_started()
        estimated_seconds = max(3.0, min(120.0, len(request.text.split()) / 1.5 + 3.0))
        deadline = time.monotonic() + estimated_seconds
        while not self._winrt_finished.wait(0.04):
            if request.cancel.is_set() or time.monotonic() >= deadline:
                self._winrt_player.pause()
                break

    def close(self) -> None:
        if self._winrt_player is not None:
            try:
                self._winrt_player.pause()
                self._winrt_player.source = None
                self._winrt_player.close()
            except Exception:
                pass
        if self._winrt_stream is not None:
            try:
                self._winrt_stream.close()
            except Exception:
                pass
        if self._winrt_synthesizer is not None:
            try:
                self._winrt_synthesizer.close()
            except Exception:
                pass
        if self._winrt_loop is not None:
            try:
                self._winrt_loop.close()
            except Exception:
                pass
        self._winrt_player = None
        self._winrt_stream = None
        self._winrt_synthesizer = None
        self._sapi_voices.clear()
        self._sapi_speaker = None


class TtsEngine:
    """Own one speech worker and one persistent Windows playback session."""

    def __init__(
        self,
        on_error: Callable[[str], None] | None = None,
        *,
        on_started: Callable[[str, float], None] | None = None,
        on_finished: Callable[[str], None] | None = None,
        initial_voice_id: str = "",
        session_factory: Callable[[], _SpeechSession] | None = None,
    ) -> None:
        self._on_error = on_error
        self._on_started = on_started
        self._on_finished = on_finished
        self._initial_voice_id = initial_voice_id
        self._session_factory = session_factory or _WindowsSpeechSession
        self._session: _SpeechSession | None = None
        self._requests: queue.Queue[_SpeechRequest | None] = queue.Queue()
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._speaking = threading.Event()
        self._current_request: _SpeechRequest | None = None
        self._current_lock = threading.Lock()
        self._generation = 0
        self._last_error: Exception | None = None
        self._error_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="native-tts", daemon=True)
        self._worker.start()

    @staticmethod
    def list_voices() -> list[Voice]:
        """List OneCore/WinRT and SAPI voices, including installed Natural voices."""

        voices: list[Voice] = []
        seen: set[tuple[str, str]] = set()
        try:
            from winrt.windows.media.speechsynthesis import SpeechSynthesizer

            for voice in SpeechSynthesizer.all_voices:
                native_id = str(voice.id)
                key = ("winrt", native_id)
                if key not in seen:
                    seen.add(key)
                    language = f" — {voice.language}" if getattr(voice, "language", "") else ""
                    voices.append(Voice(f"winrt:{native_id}", f"{voice.display_name}{language} (Windows)", "winrt", native_id))
        except Exception:
            pass

        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                collection = speaker.GetVoices()
                for index in range(collection.Count):
                    token = collection.Item(index)
                    native_id = str(token.Id)
                    key = ("sapi", native_id)
                    if key not in seen:
                        seen.add(key)
                        voices.append(Voice(f"sapi:{native_id}", f"{token.GetDescription()} (SAPI)", "sapi", native_id))
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            pass
        return voices

    def speak(self, text: str, voice_id: str = "", rate: int = 0, volume: int = 100) -> threading.Event:
        """Queue text to play and immediately return a completion event."""

        done = threading.Event()
        clean = " ".join(str(text).split())
        if not clean:
            done.set()
            return done
        with self._error_lock:
            self._last_error = None
        with self._current_lock:
            generation = self._generation
        request = _SpeechRequest(
            clean,
            voice_id,
            max(-10, min(10, int(rate))),
            max(0, min(100, int(volume))),
            generation,
            done,
        )
        self._requests.put(request)
        return done

    @property
    def last_error(self) -> Exception | None:
        with self._error_lock:
            return self._last_error

    def wait_until_idle(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._requests.unfinished_tasks == 0 and not self._speaking.is_set():
                return True
            time.sleep(0.02)
        return self._requests.unfinished_tasks == 0 and not self._speaking.is_set()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """Wait until the configured backend has finished startup prewarming."""
        return self._ready.wait(max(0.0, timeout))

    def stop(self) -> None:
        """Interrupt active speech and discard everything still waiting."""

        with self._current_lock:
            self._generation += 1
            current = self._current_request
            if current is not None:
                current.cancel.set()
        while True:
            try:
                pending = self._requests.get_nowait()
            except queue.Empty:
                break
            if pending is not None and pending.completion is not None:
                pending.cancel.set()
                pending.completion.set()
            self._requests.task_done()

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.stop()
        self._requests.put(None)
        self._worker.join(timeout=3)

    def _get_session(self) -> _SpeechSession:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def _run(self) -> None:
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except Exception:
            pythoncom = None  # type: ignore[assignment]
        try:
            if self._initial_voice_id:
                try:
                    self._get_session().prepare(self._initial_voice_id)
                except Exception:
                    # Playback reports actionable errors; startup prewarming is best-effort.
                    pass
            self._ready.set()
            while True:
                request = self._requests.get()
                try:
                    if request is None:
                        return
                    with self._current_lock:
                        if request.generation != self._generation:
                            continue
                        self._current_request = request
                    self._speaking.set()
                    try:
                        self._play(request)
                    except Exception as exc:
                        request.error = exc
                        with self._error_lock:
                            self._last_error = exc
                        if self._on_error:
                            self._on_error(str(exc))
                finally:
                    self._speaking.clear()
                    with self._current_lock:
                        if self._current_request is request:
                            self._current_request = None
                    if request is not None:
                        if self._on_finished:
                            try:
                                self._on_finished(request.text)
                            except Exception:
                                pass
                        if request.completion is not None:
                            request.completion.set()
                    self._requests.task_done()
        finally:
            self._ready.set()
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _play(self, request: _SpeechRequest) -> None:
        if request.cancel.is_set():
            return
        started = False

        def mark_started() -> None:
            nonlocal started
            if started or request.cancel.is_set():
                return
            started = True
            if self._on_started:
                try:
                    self._on_started(request.text, time.perf_counter())
                except Exception:
                    pass

        self._get_session().play(request, mark_started)


__all__ = ["TtsEngine", "TtsError", "Voice"]
