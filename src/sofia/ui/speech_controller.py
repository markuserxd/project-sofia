"""One capture or owned inference worker; no access to task/permission APIs."""

import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from sofia.speech.audio import Recording, SpeechError, validate_text
from sofia.speech.capture import AudioCapture
from sofia.speech.transcription import OfflineRecognizer, Recognizer, Transcription

MESSAGES = {
    "ready": "Microphone ready · hold Mic or focus it and hold Space",
    "initializing": "Initializing microphone…",
    "recording": "● Recording",
    "transcribing": "Transcribing locally · loading model if needed…",
    "completed": "Draft inserted · review and press Send manually",
    "draft_full": "Draft and transcript exceed 4,096 characters. Nothing inserted.",
    "cancelled": "Transcription cancelled.",
    "unavailable": "Microphone unavailable.",
    "permission_denied": "Microphone access denied. Check Windows microphone privacy settings.",
    "capture_failed": "Microphone could not start or stopped. Check device and microphone permissions.",
    "unsupported_format": "Microphone audio format is unsupported (mono/stereo PCM required).",
    "disconnected": "Microphone disconnected. Recording discarded.",
    "audio_overflow": "Audio buffer overflow. Recording discarded.",
    "too_short": "Recording too short. Hold Mic for at least 0.3 seconds.",
    "no_speech": "No speech detected. Try again.",
    "missing_model": "Offline speech model is not installed. See docs/speech-to-text.md.",
    "missing_backend": "Install the optional stt dependencies to transcribe locally.",
    "unsupported_model": "A multilingual offline speech model is required.",
    "invalid_audio": "Invalid audio. Recording discarded.",
    "invalid_transcript": "Transcript is invalid or exceeds 4,096 characters. Nothing inserted.",
    "unsupported_language": "Select Thai or English.",
    "timeout": "Transcription timed out. Waiting for the offline worker to return.",
    "transcription_failed": "Offline transcription failed. Check the installed model.",
}


class SpeechWorker(QObject):
    result = Signal(str, object, str)
    finished = Signal()

    def __init__(
        self,
        session: str,
        recognizer: Recognizer,
        audio: Recording,
        language: str,
        cancel: Event,
    ) -> None:
        super().__init__()
        self.session = session
        self.recognizer = recognizer
        self.audio: Recording | None = audio
        self.language = language
        self.cancel = cancel

    @Slot()
    def execute(self) -> None:
        result = None
        code = ""
        try:
            if self.cancel.is_set():
                raise SpeechError("cancelled")
            assert self.audio is not None
            value = self.recognizer.transcribe(self.audio, self.language, self.cancel)
            if self.cancel.is_set():
                raise SpeechError("cancelled")
            if type(value) is not Transcription or value.language != self.language:
                raise SpeechError("invalid_transcript")
            result = Transcription(validate_text(value.text), value.language)
        except SpeechError as exc:
            code = exc.code if exc.code in MESSAGES else "transcription_failed"
        except BaseException:
            code = "transcription_failed"
        finally:
            self.audio = None
            self.result.emit(self.session, result, code)
            self.finished.emit()


@dataclass
class _Inference:
    session: str
    thread: QThread
    worker: SpeechWorker
    cancel: Event
    done: bool = False
    received: bool = False
    result: Transcription | None = None
    code: str = ""


class SpeechController(QObject):
    changed = Signal()
    transcript = Signal(str)
    idle = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        capture: AudioCapture | None = None,
        recognizer: Recognizer | None = None,
    ) -> None:
        super().__init__(parent)
        self.capture = capture if capture is not None else AudioCapture(self)
        configured = os.environ.get("SOFIA_STT_MODEL_DIR")
        self.recognizer = (
            recognizer
            if recognizer is not None
            else OfflineRecognizer(Path(configured) if configured else None)
        )
        self.state = "ready" if self.capture.available() else "unavailable"
        self._session: str | None = None
        self._job: _Inference | None = None
        self._language = "th"
        self._begin = 0.0
        self._closing = False
        self._blocked = False
        self._limited = False
        self.capture.started.connect(self._started)
        self.capture.availability_changed.connect(self._availability)
        self.capture.failed.connect(self._failed)
        self.capture.limit_reached.connect(self._limit)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    @property
    def busy(self) -> bool:
        return self._session is not None or self._job is not None

    @property
    def elapsed(self) -> float:
        return max(0.0, monotonic() - self._begin)

    @property
    def limited(self) -> bool:
        return self._limited

    def reject_draft(self) -> None:
        self._set("draft_full")

    def _set(self, state: str) -> None:
        self.state = state if state in MESSAGES else "transcription_failed"
        self.changed.emit()

    def block(self, blocked: bool) -> None:
        self._blocked = blocked
        if blocked:
            self.cancel()
        self.changed.emit()

    def start(self, language: str) -> None:
        if self.busy or self._closing or self._blocked:
            return
        if not self.capture.available():
            self._set("unavailable")
            return
        if language not in ("th", "en"):
            self._set("unsupported_language")
            return
        self._session = uuid4().hex
        self._language = language
        self._limited = False
        self._begin = monotonic()
        self._set("initializing")
        self._timer.start()
        self.capture.start()

    @Slot(bool)
    def _availability(self, available: bool) -> None:
        if not self.busy and self.state in ("ready", "unavailable"):
            self._set("ready" if available else "unavailable")

    @Slot()
    def _started(self) -> None:
        if self._session and self.state == "initializing":
            self._set("recording")
        elif self.state != "recording":
            self.capture.discard()

    @Slot(str)
    def _failed(self, code: str) -> None:
        if self._session and self._job is None:
            self.capture.discard()
            self._session = None
            self._timer.stop()
            self._set(code)
            self.idle.emit()

    @Slot()
    def _limit(self) -> None:
        self._limited = True
        self.release()

    def _tick(self) -> None:
        if self._job:
            if self.elapsed >= 120 and self._session:
                self.cancel()
                self._set("timeout")
        elif self.elapsed >= 15:
            self._limit()
        self.changed.emit()

    @Slot()
    def release(self) -> None:
        if self._job or self._session is None:
            return
        if self.state != "recording":
            self.cancel()
            return
        try:
            audio = self.capture.finish()
        except SpeechError as exc:
            self._failed(exc.code)
            return
        # A synchronous capture failure during the final read invalidates this session.
        if self._session is None:
            return
        thread = QThread(self)
        cancel = Event()
        worker = SpeechWorker(
            self._session, self.recognizer, audio, self._language, cancel
        )
        worker.moveToThread(thread)
        self._job = _Inference(self._session, thread, worker, cancel)
        thread.started.connect(worker.execute)
        worker.result.connect(self._result, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._thread_done, Qt.ConnectionType.QueuedConnection)
        self._begin = monotonic()
        self._set("transcribing")
        thread.start()

    @Slot(str, object, str)
    def _result(self, session: str, result: Transcription | None, code: str) -> None:
        job = self._job
        if job and session == job.session and not job.received:
            job.received, job.result, job.code = True, result, code
            self._finish()

    @Slot()
    def _thread_done(self) -> None:
        if self._job and self.sender() is self._job.thread:
            self._job.done = True
            QTimer.singleShot(0, self._finish)

    def _finish(self) -> None:
        job = self._job
        if job is None or not job.done or not job.received:
            return
        if not job.thread.wait(0):
            QTimer.singleShot(5, self._finish)
            return
        valid = (
            self._session == job.session
            and not job.cancel.is_set()
            and not self._closing
            and not self._blocked
        )
        self._job = None
        self._session = None
        self._timer.stop()
        job.thread.deleteLater()
        if valid:
            if job.code:
                self._set(job.code)
            elif job.result:
                self._set("completed")
                self.transcript.emit(job.result.text)
        self.changed.emit()
        self.idle.emit()

    @Slot()
    def cancel(self) -> None:
        active = self.busy
        self._session = None
        self.capture.discard()
        self._timer.stop()
        if self._job:
            self._job.cancel.set()
            self._job.result = None
        if active:
            self._set("cancelled")
        if active and not self._job:
            self.idle.emit()

    def shutdown(self) -> None:
        self._closing = True
        self.cancel()
