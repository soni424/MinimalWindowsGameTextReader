"""Non-blocking Windows speech with bounded replace, queue, and overlap modes.

SAPI is used for voice discovery and (when selected) synthesis into an in-memory
WAV buffer. Playback is handled by Windows MediaPlayer sessions so repeated
captures never purge a live SAPI speaker/output device.
"""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol


# Import pywin32's COM module before any speech worker is started.  Loading the
# extension lazily from the worker can trigger pywin32 finalizers on the wrong
# thread when the app has recently created/destroyed Tk windows.  COM still
# needs to be initialized separately on each thread that uses it.
try:
    import pythoncom as _PYTHONCOM
except Exception:  # pragma: no cover - exercised only without pywin32
    _PYTHONCOM = None


CAPTURE_MODES = ("replace", "queue", "overlap")
DEFAULT_CAPTURE_MODE = "replace"
DEFAULT_MAX_OVERLAP = 2
MIN_MAX_OVERLAP = 2
MAX_MAX_OVERLAP = 4


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
    mode: str = "queue"
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
    mode: str = ""
    max_overlap: int = DEFAULT_MAX_OVERLAP


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


def _normalise_mode(value: object) -> str:
    mode = str(value).strip().lower() if isinstance(value, str) else ""
    return mode if mode in CAPTURE_MODES else DEFAULT_CAPTURE_MODE


def _normalise_max_overlap(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_MAX_OVERLAP
    return max(MIN_MAX_OVERLAP, min(MAX_MAX_OVERLAP, number))


class _WindowsSpeechSession:
    """Own one SAPI synthesizer and one Windows MediaPlayer on one worker."""

    # SAPI format 13 is 22.05 kHz, 16-bit, mono PCM. SpMemoryStream returns
    # PCM frames without a RIFF header, so the header is added before playback.
    _SAPI_FORMAT_TYPE = 13
    _SAMPLE_RATE = 22050
    _SAMPLE_WIDTH = 2
    _CHANNELS = 1

    def __init__(self) -> None:
        self._sapi_speaker = None
        self._sapi_voices: dict[str, object] = {}
        self._selected_sapi_voice = ""
        self._winrt_loop: asyncio.AbstractEventLoop | None = None
        self._winrt_synthesizer = None
        self._winrt_player = None
        self._winrt_stream = None
        self._winrt_source = None
        self._winrt_finished = threading.Event()
        self._winrt_failed = threading.Event()
        self._winrt_error = ""
        self._selected_winrt_voice = ""
        self._active_backend = ""
        self._winrt_deadline = 0.0

    def prepare(self, voice_id: str) -> None:
        # All playback uses MediaPlayer. SAPI is only a synthesizer for SAPI
        # voice tokens, which prevents the unstable live SAPI output handoff.
        self._ensure_winrt()
        if voice_id.startswith("winrt:"):
            self._ensure_winrt_synthesizer()
            self._select_winrt_voice(voice_id.removeprefix("winrt:"))
        else:
            self._ensure_sapi()
            self._select_sapi_voice(voice_id)

    def play(self, request: _SpeechRequest, on_started: Callable[[], None]) -> None:
        """Compatibility wrapper for callers that still expect blocking play."""
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
        """Synthesize and start one utterance without blocking on playback."""
        if request.cancel.is_set():
            return
        self.prepare(request.voice_id)
        if request.voice_id.startswith("winrt:"):
            self._start_winrt(request, on_started, replace)
        else:
            self._start_sapi(request, on_started, replace)

    def poll(self) -> bool:
        """Return whether the active media playback has finished."""
        if self._active_backend != "winrt":
            return True
        if self._winrt_failed.is_set():
            raise TtsError(self._winrt_error or "Windows media playback failed.")
        if self._winrt_finished.is_set():
            return True
        # The event is normally delivered by MediaPlayer. This deadline is a
        # defensive fallback for projections that miss media_ended.
        if self._winrt_deadline and time.monotonic() >= self._winrt_deadline:
            if self._winrt_player is not None:
                try:
                    self._winrt_player.pause()
                except Exception:
                    pass
            self._winrt_finished.set()
            return True
        return False

    def stop(self) -> None:
        """Stop MediaPlayer without issuing SAPI purge commands."""
        if self._winrt_player is not None:
            try:
                self._winrt_player.pause()
                self._winrt_player.source = None
            except Exception:
                pass
        self._close_stream(self._winrt_stream)
        self._winrt_stream = None
        self._winrt_source = None
        self._active_backend = ""
        self._winrt_deadline = 0.0
        self._winrt_finished.set()

    @staticmethod
    def _close_stream(stream: object | None) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except Exception:
            pass

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
        if token is None:
            raise TtsError("The selected SAPI voice is no longer installed.")
        self._sapi_speaker.Voice = token
        self._selected_sapi_voice = wanted

    def _synthesise_sapi_wav(self, request: _SpeechRequest) -> bytes:
        self._ensure_sapi()
        speaker = self._sapi_speaker
        memory_stream = None
        raw = b""
        try:
            import win32com.client

            memory_stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
            audio_format = win32com.client.Dispatch("SAPI.SpAudioFormat")
            audio_format.Type = self._SAPI_FORMAT_TYPE
            memory_stream.Format = audio_format
            speaker.Rate = request.rate
            speaker.Volume = request.volume
            speaker.AudioOutputStream = memory_stream
            # Synchronous synthesis is intentional: it writes to memory and
            # never touches the physical audio endpoint.
            speaker.Speak(request.text, 0)
            raw = bytes(memory_stream.GetData())
        except Exception as exc:
            raise TtsError(f"Windows SAPI speech synthesis failed: {exc}") from exc
        finally:
            try:
                speaker.AudioOutputStream = None
            except Exception:
                pass
            self._close_stream(memory_stream)
        if not raw:
            raise TtsError("Windows SAPI returned an empty audio buffer.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(self._CHANNELS)
            wav_file.setsampwidth(self._SAMPLE_WIDTH)
            wav_file.setframerate(self._SAMPLE_RATE)
            wav_file.writeframes(raw)
        return output.getvalue()

    def _ensure_winrt(self) -> None:
        if self._winrt_player is not None:
            return
        try:
            from winrt.windows.media.playback import MediaPlayer
            from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
        except ImportError as exc:
            raise TtsError(
                "Windows Media playback components are not installed. "
                "Install the application's requirements and try again."
            ) from exc
        self._winrt_loop = asyncio.new_event_loop()
        self._winrt_player = MediaPlayer()
        try:
            self._winrt_player.add_media_ended(self._winrt_playback_ended)
            self._winrt_player.add_media_failed(self._winrt_playback_failed)
        except AttributeError:
            # Older projections exposed events as assignable attributes.
            self._winrt_player.media_ended += self._winrt_playback_ended
            self._winrt_player.media_failed += self._winrt_playback_failed
        self._winrt_data_writer = DataWriter
        self._winrt_stream_type = InMemoryRandomAccessStream

    def _ensure_winrt_synthesizer(self) -> None:
        if self._winrt_synthesizer is not None:
            return
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer

        self._winrt_synthesizer = SpeechSynthesizer()
        self._winrt_voice_type = SpeechSynthesizer

    def _winrt_playback_ended(self, *_args: object) -> None:
        self._winrt_finished.set()

    def _winrt_playback_failed(self, *_args: object) -> None:
        self._winrt_error = "Windows MediaPlayer reported an audio failure."
        self._winrt_failed.set()
        self._winrt_finished.set()

    def _select_winrt_voice(self, native_id: str) -> None:
        if native_id == self._selected_winrt_voice:
            return
        selected = next(
            (
                voice
                for voice in self._winrt_voice_type.all_voices
                if str(voice.id) == native_id
            ),
            None,
        )
        if selected is None:
            raise TtsError("The selected Windows voice is no longer installed.")
        self._winrt_synthesizer.voice = selected
        self._selected_winrt_voice = native_id

    def _bytes_to_winrt_stream(self, data: bytes) -> object:
        if self._winrt_loop is None:
            raise TtsError("Windows media playback is not initialized.")
        stream = self._winrt_stream_type()
        writer = self._winrt_data_writer(stream.get_output_stream_at(0))
        detached = False
        try:
            writer.write_bytes(data)
            self._winrt_loop.run_until_complete(writer.store_async())
            writer.detach_stream()
            detached = True
        finally:
            if not detached:
                try:
                    writer.detach_stream()
                except Exception:
                    pass
            try:
                writer.close()
            except Exception:
                pass
        stream.seek(0)
        return stream

    def _set_media_stream(self, stream: object) -> None:
        player = self._winrt_player
        if player is None:
            raise TtsError("Windows media playback is not initialized.")
        try:
            # Current winrt projections accept the stream directly and read
            # its content type from the WAV header.
            player.set_stream_source(stream)
            self._winrt_source = None
        except TypeError:
            # Older projections require a MediaSource wrapper.
            from winrt.windows.media.core import MediaSource

            source = MediaSource.create_from_stream(stream, "audio/wav")
            player.source = source
            self._winrt_source = source

    def _start_stream(
        self,
        stream: object,
        on_started: Callable[[], None],
        replace: bool,
        duration_seconds: float,
    ) -> None:
        previous_stream = self._winrt_stream
        if replace:
            self.stop()
        self._winrt_failed.clear()
        self._winrt_error = ""
        self._winrt_finished.clear()
        self._set_media_stream(stream)
        self._winrt_stream = stream
        try:
            self._winrt_player.play()
        except Exception:
            self._close_stream(stream)
            self._winrt_stream = None
            raise
        self._active_backend = "winrt"
        self._winrt_deadline = time.monotonic() + max(0.75, duration_seconds + 1.0)
        self._close_stream(previous_stream)
        on_started()

    def _start_winrt(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None:
        if self._winrt_loop is None or self._winrt_synthesizer is None:
            raise TtsError("Windows speech synthesis is not initialized.")
        synthesizer = self._winrt_synthesizer
        synthesizer.options.speaking_rate = max(0.25, min(2.0, 1.0 + request.rate / 10.0))
        synthesizer.options.audio_volume = request.volume / 100.0
        try:
            stream = self._winrt_loop.run_until_complete(
                synthesizer.synthesize_text_to_stream_async(request.text)
            )
            if request.cancel.is_set():
                self._close_stream(stream)
                return
            duration = max(0.5, len(request.text.split()) / 2.2)
            self._start_stream(stream, on_started, replace, duration)
        except TtsError:
            raise
        except Exception as exc:
            raise TtsError(f"Windows speech synthesis/playback failed: {exc}") from exc

    def _start_sapi(
        self,
        request: _SpeechRequest,
        on_started: Callable[[], None],
        replace: bool = False,
    ) -> None:
        wav_data = self._synthesise_sapi_wav(request)
        if request.cancel.is_set():
            return
        stream = self._bytes_to_winrt_stream(wav_data)
        duration = max(0.4, (len(wav_data) - 44) / (self._SAMPLE_RATE * self._SAMPLE_WIDTH))
        try:
            self._start_stream(stream, on_started, replace, duration)
        except Exception:
            self._close_stream(stream)
            raise

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
        if self._sapi_speaker is not None:
            try:
                self._sapi_speaker.AudioOutputStream = None
            except Exception:
                pass
        self._winrt_player = None
        self._winrt_stream = None
        self._winrt_source = None
        self._winrt_synthesizer = None
        self._winrt_loop = None
        self._sapi_voices.clear()
        self._sapi_speaker = None
        self._selected_sapi_voice = ""
        self._selected_winrt_voice = ""


class TtsEngine:
    """Coordinate bounded speech playback on a dedicated worker thread."""

    def __init__(
        self,
        on_error: Callable[[str], None] | None = None,
        *,
        on_started: Callable[[str, float], None] | None = None,
        on_finished: Callable[[str], None] | None = None,
        on_started_with_id: Callable[[int, str, float], None] | None = None,
        on_finished_with_id: Callable[[int, str], None] | None = None,
        initial_voice_id: str = "",
        initial_capture_mode: str = DEFAULT_CAPTURE_MODE,
        initial_max_overlap: int = DEFAULT_MAX_OVERLAP,
        session_factory: Callable[[], _SpeechSession] | None = None,
    ) -> None:
        self._on_error = on_error
        self._on_started = on_started
        self._on_finished = on_finished
        self._on_started_with_id = on_started_with_id
        self._on_finished_with_id = on_finished_with_id
        self._initial_voice_id = initial_voice_id
        self._session_factory = session_factory or _WindowsSpeechSession
        self._requests: queue.Queue[_SpeechCommand | None] = queue.Queue(maxsize=128)
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._speaking = threading.Event()
        self._session: _SpeechSession | None = None
        self._current_request: _SpeechRequest | None = None
        self._pending_request: _SpeechRequest | None = None
        self._active_requests: dict[int, _SpeechRequest] = {}
        self._current_lock = threading.RLock()
        self._next_request_id = 0
        self._generation = 0
        self._mode = _normalise_mode(initial_capture_mode)
        self._max_overlap = _normalise_max_overlap(initial_max_overlap)
        self._last_error: Exception | None = None
        self._error_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="native-tts", daemon=True)
        self._worker.start()

    @staticmethod
    def list_voices() -> list[Voice]:
        """List OneCore/WinRT and SAPI voices, including Natural voices."""
        voices: list[Voice] = []
        seen: set[tuple[str, str]] = set()
        try:
            from winrt.windows.media.speechsynthesis import SpeechSynthesizer

            for voice in SpeechSynthesizer.all_voices:
                native_id = str(voice.id)
                key = ("winrt", native_id)
                if key in seen:
                    continue
                seen.add(key)
                language = f" — {voice.language}" if getattr(voice, "language", "") else ""
                voices.append(
                    Voice(
                        f"winrt:{native_id}",
                        f"{voice.display_name}{language} (Windows)",
                        "winrt",
                        native_id,
                    )
                )
        except Exception:
            pass

        try:
            import win32com.client

            if _PYTHONCOM is None:
                return voices
            _PYTHONCOM.CoInitialize()
            try:
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                collection = speaker.GetVoices()
                for index in range(collection.Count):
                    token = collection.Item(index)
                    native_id = str(token.Id)
                    key = ("sapi", native_id)
                    if key not in seen:
                        seen.add(key)
                        voices.append(
                            Voice(
                                f"sapi:{native_id}",
                                f"{token.GetDescription()} (SAPI)",
                                "sapi",
                                native_id,
                            )
                        )
            finally:
                _PYTHONCOM.CoUninitialize()
        except Exception:
            pass
        return voices

    @property
    def capture_mode(self) -> str:
        with self._current_lock:
            return self._mode

    @property
    def max_overlap(self) -> int:
        with self._current_lock:
            return self._max_overlap

    def set_capture_mode(self, mode: str, max_overlap: int = DEFAULT_MAX_OVERLAP) -> None:
        """Apply the playback preference immediately on the speech worker."""
        normalised = _normalise_mode(mode)
        limit = _normalise_max_overlap(max_overlap)
        with self._current_lock:
            self._mode = normalised
            self._max_overlap = limit
        if not self._shutdown.is_set():
            self._put_command(_SpeechCommand("configure", mode=normalised, max_overlap=limit))

    configure = set_capture_mode

    def _put_command(self, command: _SpeechCommand | None) -> None:
        try:
            self._requests.put_nowait(command)
            return
        except queue.Full:
            pass
        # Prefer dropping an obsolete speech command over blocking the hotkey
        # callback. Stop/configuration commands are retried after the drop.
        try:
            stale = self._requests.get_nowait()
            if stale is not None:
                self._cancel_queued(stale.request)
            self._requests.task_done()
        except queue.Empty:
            pass
        try:
            self._requests.put_nowait(command)
        except queue.Full:
            if command is not None:
                self._cancel_queued(command.request)

    def _submit(
        self,
        text: str,
        voice_id: str,
        rate: int,
        volume: int,
        *,
        mode: str,
    ) -> SpeechTicket:
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
            voice_id=str(voice_id or ""),
            rate=max(-10, min(10, int(rate))),
            volume=max(0, min(100, int(volume))),
            mode=_normalise_mode(mode),
            generation=generation,
            completion=done,
        )
        if self._shutdown.is_set():
            request.cancel.set()
            done.set()
            return done
        self._put_command(
            _SpeechCommand(
                "speak",
                request,
                replace=request.mode == "replace",
                mode=request.mode,
            )
        )
        return done

    def speak(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        """Queue one line using the legacy sequential API."""
        return self._submit(text, voice_id, rate, volume, mode="queue")

    def enqueue(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        return self._submit(text, voice_id, rate, volume, mode="queue")

    def replace(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        return self._submit(text, voice_id, rate, volume, mode="replace")

    def overlap(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
    ) -> SpeechTicket:
        return self._submit(text, voice_id, rate, volume, mode="overlap")

    def speak_mode(
        self,
        text: str,
        voice_id: str = "",
        rate: int = 0,
        volume: int = 100,
        mode: str | None = None,
    ) -> SpeechTicket:
        selected = mode or self.capture_mode
        if selected == "overlap":
            return self.overlap(text, voice_id, rate, volume)
        if selected == "replace":
            return self.replace(text, voice_id, rate, volume)
        return self.enqueue(text, voice_id, rate, volume)

    @property
    def last_error(self) -> Exception | None:
        with self._error_lock:
            return self._last_error

    def wait_until_idle(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._current_lock:
                active = bool(self._active_requests)
                pending = self._pending_request is not None
            if self._requests.unfinished_tasks == 0 and not active and not pending and not self._speaking.is_set():
                return True
            time.sleep(0.02)
        with self._current_lock:
            active = bool(self._active_requests)
            pending = self._pending_request is not None
        return self._requests.unfinished_tasks == 0 and not active and not pending and not self._speaking.is_set()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(max(0.0, timeout))

    def stop(self) -> None:
        """Interrupt all active playback and discard waiting speech."""
        with self._current_lock:
            self._generation += 1
            current_requests = list(self._active_requests.values())
            if self._current_request is not None and self._current_request not in current_requests:
                current_requests.append(self._current_request)
            pending = self._pending_request
            self._pending_request = None
            for request in current_requests:
                request.cancel.set()
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
            self._put_command(_SpeechCommand("stop"))

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.stop()
        self._put_command(None)
        self._worker.join(timeout=3)

    def _get_session(self) -> _SpeechSession:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    @staticmethod
    def _supports_nonblocking(session: object) -> bool:
        return all(callable(getattr(session, name, None)) for name in ("start", "poll", "stop"))

    def _set_active(self, request: _SpeechRequest | None) -> None:
        with self._current_lock:
            if self._current_request is not None:
                self._active_requests.pop(self._current_request.request_id, None)
            self._current_request = request
            if request is not None:
                self._active_requests[request.request_id] = request
            active = bool(self._active_requests)
        if active:
            self._speaking.set()
        else:
            self._speaking.clear()

    def _register_overlap(self, request: _SpeechRequest) -> None:
        with self._current_lock:
            self._active_requests[request.request_id] = request
            self._speaking.set()

    def _unregister_overlap(self, request: _SpeechRequest) -> None:
        with self._current_lock:
            self._active_requests.pop(request.request_id, None)
            active = bool(self._active_requests)
        if not active:
            self._speaking.clear()

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

    def _start_on_session(
        self,
        request: _SpeechRequest,
        session: _SpeechSession,
        *,
        replace: bool,
    ) -> tuple[_SpeechSession | None, bool]:
        """Start a request, recreating the native session once on failure."""
        for attempt in range(2):
            with self._current_lock:
                stale = request.generation != self._generation
            if request.cancel.is_set() or stale:
                self._cancel_queued(request)
                return session, False
            started = False

            def mark_started() -> None:
                nonlocal started
                if started or request.cancel.is_set():
                    return
                started = True
                self._notify_started(request)

            try:
                legacy_override = type(self)._play is not TtsEngine._play
                nonblocking = self._supports_nonblocking(session) and not legacy_override
                if nonblocking:
                    session.start(request, mark_started, replace)
                    return session, True
                self._play(request)
                return session, False
            except Exception as exc:
                if started or attempt:
                    self._report_error(request, exc)
                    self._finish_request(request)
                    try:
                        session.close()
                    except Exception:
                        pass
                    return None, False
                try:
                    session.close()
                except Exception:
                    pass
                try:
                    session = self._session_factory()
                except Exception as recreate_exc:
                    self._report_error(request, recreate_exc)
                    self._finish_request(request)
                    return None, False
        return None, False

    def _start_request(self, request: _SpeechRequest, *, replace: bool = False) -> _SpeechRequest | None:
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
        self._set_active(request)
        session, started = self._start_on_session(request, session, replace=replace)
        if session is not None:
            self._session = session
        if not started:
            self._finish_request(request)
            self._set_active(None)
            return None
        # A blocking compatibility session is already complete.
        if not self._supports_nonblocking(session) or type(self)._play is not TtsEngine._play:
            self._finish_request(request)
            self._set_active(None)
            return None
        return request

    def _start_overlap_request(
        self,
        request: _SpeechRequest,
    ) -> tuple[_SpeechRequest, _SpeechSession] | None:
        try:
            session = self._session_factory()
        except Exception as exc:
            self._report_error(request, exc)
            self._cancel_queued(request)
            return None
        self._register_overlap(request)
        session, started = self._start_on_session(request, session, replace=False)
        if session is None or not started:
            self._unregister_overlap(request)
            self._finish_request(request)
            return None
        if not self._supports_nonblocking(session):
            self._unregister_overlap(request)
            self._finish_request(request)
            return None
        return request, session

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

    def _finish_overlap(
        self,
        request: _SpeechRequest,
        session: _SpeechSession,
        *,
        stop_backend: bool = False,
    ) -> None:
        if stop_backend:
            try:
                session.stop()
            except Exception:
                pass
        self._finish_request(request)
        self._unregister_overlap(request)
        try:
            session.close()
        except Exception:
            pass

    def _run(self) -> None:
        pythoncom = _PYTHONCOM
        if pythoncom is not None:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pythoncom = None  # type: ignore[assignment]
        try:
            if self._initial_voice_id:
                try:
                    self._get_session().prepare(self._initial_voice_id)
                except Exception:
                    pass
            self._ready.set()
            active: _SpeechRequest | None = None
            pending: _SpeechRequest | None = None
            overlap_active: dict[int, tuple[_SpeechRequest, _SpeechSession]] = {}
            overlap_pending: deque[_SpeechRequest] = deque()
            mode = self.capture_mode
            max_overlap = self.max_overlap
            while True:
                received = False
                command: _SpeechCommand | None = None
                busy = active is not None or bool(overlap_active)
                try:
                    command = self._requests.get(timeout=0.02 if busy else None)
                    received = True
                except queue.Empty:
                    pass
                if received:
                    try:
                        if command is None:
                            self._cancel_queued(pending)
                            for request in overlap_pending:
                                self._cancel_queued(request)
                            self._finish_active(active, stop_backend=True)
                            for request, session in list(overlap_active.values()):
                                self._finish_overlap(request, session, stop_backend=True)
                            return
                        if command.kind == "configure":
                            mode = _normalise_mode(command.mode)
                            max_overlap = _normalise_max_overlap(command.max_overlap)
                            # A mode change is a clear boundary. Closing active
                            # sessions prevents old overlap from leaking into a
                            # newly selected no-overlap mode.
                            self._cancel_queued(pending)
                            pending = None
                            self._set_pending(None)
                            for request in overlap_pending:
                                self._cancel_queued(request)
                            overlap_pending.clear()
                            self._finish_active(active, stop_backend=True)
                            active = None
                            for request, session in list(overlap_active.values()):
                                self._finish_overlap(request, session, stop_backend=True)
                            overlap_active.clear()
                        elif command.kind == "stop":
                            self._cancel_queued(pending)
                            pending = None
                            self._set_pending(None)
                            for request in overlap_pending:
                                self._cancel_queued(request)
                            overlap_pending.clear()
                            self._finish_active(active, stop_backend=True)
                            active = None
                            for request, session in list(overlap_active.values()):
                                self._finish_overlap(request, session, stop_backend=True)
                            overlap_active.clear()
                        elif command.kind == "speak" and command.request is not None:
                            request = command.request
                            with self._current_lock:
                                stale = request.generation != self._generation
                            if stale or request.cancel.is_set():
                                self._cancel_queued(request)
                            elif request.mode == "overlap":
                                if len(overlap_active) < max_overlap:
                                    started = self._start_overlap_request(request)
                                    if started is not None:
                                        overlap_active[request.request_id] = started
                                else:
                                    while len(overlap_pending) >= max_overlap:
                                        self._cancel_queued(overlap_pending.popleft())
                                    overlap_pending.append(request)
                            elif active is None:
                                active = self._start_request(request, replace=request.mode == "replace")
                            elif request.mode == "replace":
                                self._cancel_queued(pending)
                                pending = None
                                self._set_pending(None)
                                self._finish_active(active, stop_backend=True)
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

                for request_id, (request, session) in list(overlap_active.items()):
                    with self._current_lock:
                        stale = request.generation != self._generation
                    if request.cancel.is_set() or stale:
                        self._finish_overlap(request, session, stop_backend=True)
                        overlap_active.pop(request_id, None)
                        continue
                    try:
                        finished = bool(session.poll())
                    except Exception as exc:
                        self._report_error(request, exc)
                        finished = True
                    if finished:
                        self._finish_overlap(request, session)
                        overlap_active.pop(request_id, None)

                while overlap_pending and len(overlap_active) < max_overlap:
                    next_request = overlap_pending.popleft()
                    started = self._start_overlap_request(next_request)
                    if started is not None:
                        overlap_active[next_request.request_id] = started

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


__all__ = [
    "CAPTURE_MODES",
    "DEFAULT_CAPTURE_MODE",
    "DEFAULT_MAX_OVERLAP",
    "MAX_MAX_OVERLAP",
    "MIN_MAX_OVERLAP",
    "SpeechTicket",
    "TtsEngine",
    "TtsError",
    "Voice",
]
