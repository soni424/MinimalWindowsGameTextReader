"""Single reusable newest-job worker for screen capture, OCR, and correction."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class CaptureJob:
    identifier: int
    box: tuple[int, int, int, int]
    source: str
    settings: Mapping[str, Any]
    requested_at: float
    on_capture_complete: Callable[[], None] | None = None


@dataclass(frozen=True)
class PipelineTimings:
    """Monotonic boundaries for shortcut-to-corrected-text profiling."""

    requested_at: float
    worker_started_at: float
    capture_finished_at: float
    ocr_finished_at: float
    correction_finished_at: float

    @property
    def dispatch_ms(self) -> float:
        return max(0.0, (self.worker_started_at - self.requested_at) * 1000.0)

    @property
    def capture_ms(self) -> float:
        return max(0.0, (self.capture_finished_at - self.worker_started_at) * 1000.0)

    @property
    def ocr_ms(self) -> float:
        return max(0.0, (self.ocr_finished_at - self.capture_finished_at) * 1000.0)

    @property
    def correction_ms(self) -> float:
        return max(0.0, (self.correction_finished_at - self.ocr_finished_at) * 1000.0)

    @property
    def ready_ms(self) -> float:
        return max(0.0, (self.correction_finished_at - self.requested_at) * 1000.0)


class CaptureWorker:
    """Serialize OCR and replace pending work so stale game dialogue never wins."""

    def __init__(
        self,
        capture: Callable[[list[int]], Any],
        recognise: Callable[[Any], str],
        correct: Callable[[str, Mapping[str, Any]], Any],
        on_result: Callable[[CaptureJob, Any, PipelineTimings], None],
        on_error: Callable[[CaptureJob, Exception], None],
        on_start: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._capture = capture
        self._recognise = recognise
        self._correct = correct
        self._on_result = on_result
        self._on_error = on_error
        self._on_start = on_start
        self._on_close = on_close
        self._condition = threading.Condition()
        self._pending: CaptureJob | None = None
        self._active: CaptureJob | None = None
        self._latest_identifier = 0
        self._closed = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="screen-ocr-worker", daemon=True)
        self._thread.start()

    def submit(
        self,
        box: list[int] | tuple[int, int, int, int],
        source: str,
        settings: Mapping[str, Any],
        requested_at: float | None = None,
        on_capture_complete: Callable[[], None] | None = None,
    ) -> int:
        if len(box) != 4 or int(box[2]) <= int(box[0]) or int(box[3]) <= int(box[1]):
            raise ValueError("The selected capture region is invalid.")
        with self._condition:
            if self._closed:
                raise RuntimeError("The capture worker has stopped.")
            self._latest_identifier += 1
            job = CaptureJob(
                self._latest_identifier,
                tuple(int(part) for part in box),
                str(source),
                copy.deepcopy(dict(settings)),
                requested_at if requested_at is not None else time.perf_counter(),
                on_capture_complete,
            )
            self._pending = job
            self._condition.notify_all()
            return job.identifier

    def _is_latest(self, identifier: int) -> bool:
        with self._condition:
            return not self._closed and identifier == self._latest_identifier

    def _run(self) -> None:
        try:
            try:
                if self._on_start is not None:
                    self._on_start()
            except Exception:
                # The first real request reports initialization failures with
                # its source context; prewarming must not kill the worker.
                pass
            finally:
                self._ready.set()
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    job = self._pending
                    self._pending = None
                    self._active = job
                assert job is not None
                try:
                    self._process(job)
                finally:
                    with self._condition:
                        if self._active is job:
                            self._active = None
                        self._condition.notify_all()
        finally:
            if self._on_close is not None:
                try:
                    self._on_close()
                except Exception:
                    pass

    def _process(self, job: CaptureJob) -> None:
        worker_started = time.perf_counter()
        try:
            try:
                image = self._capture(list(job.box))
            finally:
                if job.on_capture_complete is not None:
                    job.on_capture_complete()
            capture_finished = time.perf_counter()
            if not self._is_latest(job.identifier):
                return
            raw_text = self._recognise(image)
            ocr_finished = time.perf_counter()
            if not self._is_latest(job.identifier):
                return
            result = self._correct(raw_text, job.settings)
            correction_finished = time.perf_counter()
            if not self._is_latest(job.identifier):
                return
            timings = PipelineTimings(
                job.requested_at,
                worker_started,
                capture_finished,
                ocr_finished,
                correction_finished,
            )
            # Linearize publication with ``submit``. Either this result wins
            # before the new request is accepted, or the generation check drops
            # it; there is no gap where stale speech can be queued afterward.
            with self._condition:
                if self._closed or job.identifier != self._latest_identifier:
                    return
                self._on_result(job, result, timings)
        except Exception as exc:
            with self._condition:
                if not self._closed and job.identifier == self._latest_identifier:
                    self._on_error(job, exc)

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while (self._pending is not None or self._active is not None) and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._pending is None and self._active is None

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """Wait until one-time worker resource initialization has finished."""
        return self._ready.wait(max(0.0, timeout))

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._latest_identifier += 1
            self._pending = None
            self._condition.notify_all()
        self._thread.join(timeout=3)


__all__ = ["CaptureJob", "CaptureWorker", "PipelineTimings"]
