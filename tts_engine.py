"""Non-blocking native Windows speech synthesis with a robust SAPI fallback."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


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
    completion: threading.Event | None = None
    error: Exception | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)


class TtsEngine:
    """Queue speech work so UI and global hotkey hooks never block on audio."""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self._on_error = on_error
        self._requests: queue.Queue[_SpeechRequest | None] = queue.Queue()
        self._shutdown = threading.Event()
        self._speaking = threading.Event()
        self._current_request: _SpeechRequest | None = None
        self._current_lock = threading.Lock()
        self._last_error: Exception | None = None
        self._error_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="native-tts", daemon=True)
        self._worker.start()

    @staticmethod
    def list_voices() -> list[Voice]:
        """List OneCore/WinRT voices and SAPI voices, including Natural voices."""
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
            # The WinRT voice catalog can be unavailable on stripped-down
            # Windows installs; SAPI remains a fully functional fallback.
            pass

        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                for index in range(speaker.GetVoices().Count):
                    token = speaker.GetVoices().Item(index)
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
        request = _SpeechRequest(clean, voice_id, max(-10, min(10, int(rate))), max(0, min(100, int(volume))), done)
        self._requests.put(request)
        return done

    @property
    def last_error(self) -> Exception | None:
        """Return the most recent asynchronous playback error, if any."""
        with self._error_lock:
            return self._last_error

    def wait_until_idle(self, timeout: float = 15.0) -> bool:
        """Wait for queued playback to complete; useful to automated tests only."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._requests.unfinished_tasks == 0 and not self._speaking.is_set():
                return True
            time.sleep(0.02)
        return self._requests.unfinished_tasks == 0 and not self._speaking.is_set()

    def stop(self) -> None:
        """Interrupt active speech and discard anything still waiting to play."""
        with self._current_lock:
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
        """Stop the worker thread after its current request has completed."""
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.stop()
        self._requests.put(None)
        self._worker.join(timeout=3)

    def _run(self) -> None:
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except Exception:
            pythoncom = None  # type: ignore[assignment]
        try:
            while True:
                request = self._requests.get()
                try:
                    if request is None:
                        return
                    with self._current_lock:
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
                    if request is not None and request.completion is not None:
                        request.completion.set()
                    self._requests.task_done()
        finally:
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _play(self, request: _SpeechRequest) -> None:
        """Prefer WinRT for explicitly selected OneCore voices, then use SAPI."""
        if request.cancel.is_set():
            return
        if request.voice_id.startswith("winrt:"):
            try:
                self._play_winrt(request)
                return
            except Exception:
                # A voice can be uninstalled between enumeration and playback.
                # SAPI receives the same text instead of silently dropping it.
                pass
        self._play_sapi(request)

    @staticmethod
    def _play_winrt(request: _SpeechRequest) -> None:
        """Synthesize and play one OneCore voice using Windows Media APIs."""
        from winrt.windows.media.core import MediaSource
        from winrt.windows.media.playback import MediaPlayer
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer

        target_id = request.voice_id.removeprefix("winrt:")
        selected = next((voice for voice in SpeechSynthesizer.all_voices if str(voice.id) == target_id), None)
        if selected is None:
            raise TtsError("The selected Windows voice is no longer installed.")
        synthesizer = SpeechSynthesizer()
        synthesizer.voice = selected
        # WinRT accepts a multiplier for speaking rate, while the UI presents
        # familiar SAPI-style -10…10 settings.
        synthesizer.options.speaking_rate = max(0.25, min(2.0, 1.0 + request.rate / 10.0))
        synthesizer.options.audio_volume = request.volume / 100.0
        stream = asyncio.run(synthesizer.synthesize_text_to_stream_async(request.text))
        player = MediaPlayer()
        finished = threading.Event()
        player.media_ended += lambda *_: finished.set()
        player.media_failed += lambda *_: finished.set()
        player.source = MediaSource.create_from_stream(stream, stream.content_type)
        player.play()
        # MediaPlayer delivers events on Windows' media thread.  This generous
        # bounded wait keeps the objects alive for playback without freezing UI.
        estimated_seconds = max(3.0, min(120.0, len(request.text.split()) / 1.5 + 3.0))
        deadline = time.monotonic() + estimated_seconds
        while not finished.wait(0.05):
            if request.cancel.is_set() or time.monotonic() >= deadline:
                player.pause()
                break
        player.close()
        synthesizer.close()

    @staticmethod
    def _play_sapi(request: _SpeechRequest) -> None:
        """Speak synchronously on this worker's thread through classic SAPI5."""
        try:
            import win32com.client

            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Rate = request.rate
            speaker.Volume = request.volume
            if request.voice_id.startswith("sapi:"):
                wanted = request.voice_id.removeprefix("sapi:")
                for index in range(speaker.GetVoices().Count):
                    token = speaker.GetVoices().Item(index)
                    if str(token.Id) == wanted:
                        speaker.Voice = token
                        break
            # Async playback lets this same COM thread purge the utterance when
            # a new capture arrives or the user presses Stop audio.
            speaker.Speak(request.text, 1)  # SVSFlagsAsync
            while not speaker.WaitUntilDone(100):
                if request.cancel.is_set():
                    speaker.Speak("", 3)  # SVSFlagsAsync | SVSFPurgeBeforeSpeak
                    break
        except ImportError as exc:
            raise TtsError("pywin32/SAPI5 is not available on this Windows installation.") from exc
        except Exception as exc:
            raise TtsError(f"Windows SAPI playback failed: {exc}") from exc
