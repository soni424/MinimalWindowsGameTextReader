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
    request_id: int
    text: str
    voice_id: str
    rate: int
    volume: int
    generation: int = 0
    completion: threading.Event | None = None
    error: Exception | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)


class SpeechTicket(threading.Event):
    """Completion event returned for one queued speech request."""

    def __init__(self, request_id: int) -> None:
        super().__init__()
        self.request_id = request_id


@dataclass(frozen=True)
class _SpeechCommand:
    kind: str
    request: _SpeechRequest | None = None
    replace: bool = False


class _SpeechSession(Protocol):
    def prepare(self, voice_id: str) -> None: ...

    def play(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None: ...

    def start(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None: ...

    def poll(self) -> bool: ...

    def stop(self) -> None: ...

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
        self._active_backend = ""
        self._winrt_deadline = 0.0

    def prepare(self, voice_id: str) -> None:
        if voice_id.startswith("winrt:"):
            self._ensure_winrt()
            self._select_winrt_voice(voice_id.removeprefix("winrt:"))
        else:
            self._ensure_sapi()
            self._select_sapi_voice(voice_id)

    def play(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None:
        """Compatibility wrapper for callers that still expect blocking playback."""
        self.start(request, on_started)
        while not self.poll():
            if request.cancel.is_set():
                self.stop()
                return
            time.sleep(0.04)

    def start(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None:
        """Start one utterance without blocking the speech worker."""
        if request.cancel.is_set():
            return
        if request.voice_id.startswith("winrt:"):
            if self._active_backend == "sapi":
                self.stop()
            playback_started = False

            def winrt_started() -> None:
                nonlocal playback_started
                playback_started = True
                on_started()

            try:
                self.prepare(request.voice_id)
                self._start_winrt(request, winrt_started, replace)
                return
            except Exception:
                if playback_started:
                    raise
                # A OneCore voice may disappear between enumeration and use.
                # The persistent SAPI session still provides audible output.
                if self._active_backend == "winrt":
                    self.stop()
        elif self._active_backend == "winrt":
            self.stop()
        self.prepare(request.voice_id if request.voice_id.startswith("sapi:") else "")
        self._start_sapi(request, on_started, replace)

    def poll(self) -> bool:
        """Return whether the active native playback has finished."""
        if self._active_backend == "sapi":
            speaker = self._sapi_speaker
            if speaker is None:
                return True
            try:
                # A short timeout keeps the command loop responsive to the next
                # capture while avoiding a busy-spin on the COM object.
                return bool(speaker.WaitUntilDone(10))
            except Exception as exc:
                raise TtsError(f"Windows SAPI playback status failed: {exc}") from exc
        if self._active_backend == "winrt":
            if self._winrt_finished.is_set():
                return True
            if self._winrt_deadline and time.monotonic() >= self._winrt_deadline:
                if self._winrt_player is not None:
                    try:
                        self._winrt_player.pause()
                    except Exception:
                        pass
                self._winrt_finished.set()
                return True
            return False
        return True

    def stop(self) -> None:
        """Purge the active native playback for an explicit stop command."""
        if self._active_backend == "sapi" and self._sapi_speaker is not None:
            try:
                self._sapi_speaker.Speak("", 2)  # SVSFPurgeBeforeSpeak
            except Exception:
                pass
        elif self._active_backend == "winrt":
            if self._winrt_player is not None:
                try:
                    self._winrt_player.pause()
                    self._winrt_player.source = None
                except Exception:
                    pass
            if self._winrt_stream is not None:
                try:
                    self._winrt_stream.close()
                except Exception:
                    pass
            self._winrt_stream = None
        self._active_backend = ""
        self._winrt_deadline = 0.0
        self._winrt_finished.set()

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

    def _start_sapi(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None:
        speaker = self._sapi_speaker
        try:
            speaker.Rate = request.rate
            speaker.Volume = request.volume
            # SVSFlagsAsync keeps this call non-blocking. When replacing a
            # trailing/active utterance, SVSFPurgeBeforeSpeak makes the purge
            # and new utterance one native operation instead of an audible
            # empty purge followed by a second Speak call.
            speaker.Speak(request.text, 1 | (2 if replace else 0))
            self._active_backend = "sapi"
            on_started()
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

    def _start_winrt(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None:
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
        if replace and self._winrt_player is not None:
            try:
                self._winrt_player.pause()
                self._winrt_player.source = None
            except Exception:
                pass
        self._winrt_stream = stream
        self._winrt_finished.clear()
        self._winrt_player.source = source
        if previous_stream is not None:
            try:
                previous_stream.close()
            except Exception:
                pass
        self._winrt_player.play()
        self._active_backend = "winrt"
        on_started()
        estimated_seconds = max(3.0, min(120.0, len(request.text.split()) / 1.5 + 3.0))
        self._winrt_deadline = time.monotonic() + estimated_seconds

    def close(self) -> None:
        self.stop()
        if self._winrt_player is not None:
            try:
                self._winrt_player.close()
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
        self._selected_sapi_voice = ""
        self._selected_winrt_voice = ""


class TtsEngine:
    """Own one speech command worker and one persistent Windows playback session.

    The worker keeps native speech resources on one thread, but the command
    loop itself remains responsive while audio is playing. That lets a newer
    capture replace a trailing utterance immediately instead of waiting for a
    backend cleanup timeout.
    """

    def __init__(
        self,
        on_error: Callable[[str], None] | None = None,
        *,
        on_started: Callable[[str, float], None] | None = None,
        on_finished: Callable[[str], None] | None = None,
        on_started_with_id: Callable[[int, str, float], None] | None = None,
        on_finished_with_id: Callable[[int, str], None] | None = None,
        initial_voice_id: str = "",
        session_factory: Callable[[], _SpeechSession] | None = None,
    ) -> None:
        self._on_error = on_error
        self._on_started = on_started
        self._on_finished = on_finished
        self._on_started_with_id = on_started_with_id
        self._on_finished_with_id = on_finished_with_id
        self._initial_voice_id = initial_voice_id
        self._session_factory = session_factory or _WindowsSpeechSession
        self._session: _SpeechSession | None = None
        self._requests: queue.Queue[_SpeechCommand | None] = queue.Queue()
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._speaking = threading.Event()
        self._current_request: _SpeechRequest | None = None
        self._pending_request: _SpeechRequest | None = None
        self._current_lock = threading.RLock()
        self._next_request_id = 0
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

    def _submit(
        self,
        text: str,
        voice_id: str,
        rate: int,
        volume: int,
        *,
        replace: bool,
    ) -> SpeechTicket:
        """Create a bounded speech command without touching native resources."""
        clean = " ".join(str(text).split())
        with self._current_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            generation = self._generation
        done = SpeechTicket(request_id)
        if not clean:
            done.set()
            return done
        with self._error_lock:
            self._last_error = None
        request = _SpeechRequest(
            request_id=request_id,
            text=clean,
            voice_id=voice_id,
            rate=max(-10, min(10, int(rate))),
            volume=max(0, min(100, int(volume))),
            generation=generation,
            completion=done,
        )
        if self._shutdown.is_set():
            request.cancel.set()
            done.set()
            return done
        self._requests.put(_SpeechCommand("speak", request, replace))
        return done

    def speak(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        """Queue one line and immediately return its completion ticket."""
        return self._submit(text, voice_id, rate, volume, replace=False)

    def enqueue(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        """Queue one prepared line behind the current utterance."""
        return self._submit(text, voice_id, rate, volume, replace=False)

    def replace(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        """Replace active/pending speech as soon as the backend accepts it."""
        return self._submit(text, voice_id, rate, volume, replace=True)

    @property
    def last_error(self) -> Exception | None:
        with self._error_lock:
            return self._last_error

    def wait_until_idle(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._current_lock:
                active = self._current_request
                pending = self._pending_request
            if (
                self._requests.unfinished_tasks == 0
                and active is None
                and pending is None
                and not self._speaking.is_set()
            ):
                return True
            time.sleep(0.02)
        with self._current_lock:
            active = self._current_request
            pending = self._pending_request
        return (
            self._requests.unfinished_tasks == 0
            and active is None
            and pending is None
            and not self._speaking.is_set()
        )

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
            pending = self._pending_request
            self._pending_request = None
        self._cancel_queued(pending)
        while True:
            try:
                command = self._requests.get_nowait()
            except queue.Empty:
                break
            if command is not None:
                self._cancel_queued(command.request)
            self._requests.task_done()
        if not self._shutdown.is_set():
            self._requests.put(_SpeechCommand("stop"))

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

    @staticmethod
    def _supports_nonblocking(session: object) -> bool:
        return all(
            callable(getattr(session, name, None))
            for name in ("start", "poll", "stop")
        )

    def _set_active(self, request: _SpeechRequest | None) -> None:
        with self._current_lock:
            self._current_request = request
        if request is None:
            self._speaking.clear()
        else:
            self._speaking.set()

    def _set_pending(self, request: _SpeechRequest | None) -> None:
        with self._current_lock:
            self._pending_request = request

    @staticmethod
    def _cancel_queued(request: _SpeechRequest | None) -> None:
        if request is None:
            return
        request.cancel.set()
        if request.completion is not None:
            request.completion.set()

    def _finish_request(self, request: _SpeechRequest) -> None:
        if request.completion is not None and request.completion.is_set():
            return
        if self._on_finished:
            try:
                self._on_finished(request.text)
            except Exception:
                pass
        if self._on_finished_with_id:
            try:
                self._on_finished_with_id(request.request_id, request.text)
            except Exception:
                pass
        if request.completion is not None:
            request.completion.set()

    def _notify_started(self, request: _SpeechRequest) -> None:
        started_at = time.perf_counter()
        if self._on_started:
            try:
                self._on_started(request.text, started_at)
            except Exception:
                pass
        if self._on_started_with_id:
            try:
                self._on_started_with_id(request.request_id, request.text, started_at)
            except Exception:
                pass

    def _report_error(self, request: _SpeechRequest, exc: Exception) -> None:
        request.error = exc
        with self._error_lock:
            self._last_error = exc
        if self._on_error:
            try:
                self._on_error(str(exc))
            except Exception:
                pass

    def _start_request(
        self,
        request: _SpeechRequest,
        *,
        replace: bool = False,
    ) -> _SpeechRequest | None:
        with self._current_lock:
            if request.cancel.is_set() or request.generation != self._generation:
                self._cancel_queued(request)
                return None
        try:
            session = self._get_session()
        except Exception as exc:
            self._report_error(request, exc)
            self._cancel_queued(request)
            return None
        # Preserve the legacy extension seam used by custom engines that
        # override ``_play`` (for example, test doubles and integrations).
        legacy_override = type(self)._play is not TtsEngine._play
        nonblocking = self._supports_nonblocking(session) and not legacy_override
        self._set_active(request)
        started = False

        def mark_started() -> None:
            nonlocal started
            if started or request.cancel.is_set():
                return
            started = True
            self._notify_started(request)

        try:
            if nonblocking:
                session.start(request, mark_started, replace)
                return request
            # Keep the old hook for compatible custom sessions and tests.
            self._play(request)
        except Exception as exc:
            self._report_error(request, exc)
            if nonblocking:
                self._finish_request(request)
                self._set_active(None)
                return None
        finally:
            if not nonblocking:
                self._finish_request(request)
                self._set_active(None)
        return request if nonblocking else None

    def _finish_active(
        self,
        active: _SpeechRequest | None,
        *,
        stop_backend: bool = False,
    ) -> None:
        if active is None:
            return
        session = self._session
        if stop_backend and session is not None and self._supports_nonblocking(session):
            try:
                session.stop()
            except Exception:
                pass
        self._finish_request(active)
        self._set_active(None)

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
            active: _SpeechRequest | None = None
            pending: _SpeechRequest | None = None
            while True:
                received = False
                command: _SpeechCommand | None = None
                try:
                    command = self._requests.get(timeout=0.02 if active is not None else None)
                    received = True
                except queue.Empty:
                    pass
                if received:
                    try:
                        if command is None:
                            self._finish_active(active, stop_backend=True)
                            self._cancel_queued(pending)
                            return
                        if command.kind == "stop":
                            self._cancel_queued(pending)
                            pending = None
                            self._set_pending(None)
                            self._finish_active(active, stop_backend=True)
                            active = None
                        elif command.kind == "speak" and command.request is not None:
                            request = command.request
                            with self._current_lock:
                                stale = request.generation != self._generation
                            if stale or request.cancel.is_set():
                                self._cancel_queued(request)
                            elif active is None:
                                active = self._start_request(request, replace=command.replace)
                            elif command.replace:
                                self._cancel_queued(pending)
                                pending = None
                                self._set_pending(None)
                                self._finish_active(active)
                                active = self._start_request(request, replace=True)
                            else:
                                self._cancel_queued(pending)
                                pending = request
                                self._set_pending(request)
                    finally:
                        self._requests.task_done()

                if active is not None:
                    with self._current_lock:
                        stale = active.generation != self._generation
                    if active.cancel.is_set() or stale:
                        self._finish_active(active, stop_backend=True)
                        active = None
                    elif self._session is not None and self._supports_nonblocking(self._session):
                        try:
                            finished = bool(self._session.poll())
                        except Exception as exc:
                            self._report_error(active, exc)
                            finished = True
                        if finished:
                            self._finish_active(active)
                            active = None

                if active is None and pending is not None:
                    next_request = pending
                    pending = None
                    self._set_pending(None)
                    active = self._start_request(next_request)
        finally:
            self._ready.set()
            self._set_active(None)
            self._set_pending(None)
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
            self._notify_started(request)

        self._get_session().play(request, mark_started)


__all__ = ["SpeechTicket", "TtsEngine", "TtsError", "Voice"]
