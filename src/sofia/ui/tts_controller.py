"""GUI-owned output queue; no tool, approval, provider or microphone authority."""

import logging
from collections.abc import Callable
from time import monotonic

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from sofia.tts.engine import TTSEngine
from sofia.tts.models import ERRORS, Capabilities, TTSConfig, TTSError, TTSRequest
from sofia.tts.queue import PlaybackQueue

MESSAGES = {
    "idle": "Speaker idle · enable playback in Settings",
    "queued": "Speech queued",
    "preparing": "Preparing local speech…",
    "synthesizing": "Synthesizing locally…",
    "playing": "Speaking · local engine",
    "stopping": "Stopping speech · waiting for the engine",
    "completed": "Speech completed (engine reported)",
    "cancelled": "Speech stopped",
    "invalid_text": "Use 1–1,000 characters of plain text; no markup, controls, paths or URL-only requests.",
    "unsupported_language": "Selected voice cannot read this language. Select Thai or English explicitly.",
    "mixed_language": "Mixed Thai/English playback is unavailable. Use separate language-specific sentences.",
    "invalid_settings": "Voice settings are invalid or unsupported.",
    "unknown_engine": "Selected local speech engine is unavailable.",
    "missing_voice": "Selected voice is not installed for this language.",
    "unsupported_pitch": "Pitch adjustment is unavailable for this backend.",
    "unsupported_rate": "This engine cannot apply the selected speaking rate.",
    "unavailable": "Local voice or audio output unavailable. See docs/text-to-speech.md.",
    "playback_failed": "Local speech failed. Check voices and audio output.",
    "queue_full": "Speech queue is full (3 pending / 3,000 total characters).",
    "disabled": "Enable voice playback in Settings first.",
    "blocked": "Speech is blocked during recording, tasks, approvals or shutdown.",
    "timeout": "Speech timed out; stopping output.",
    "stop_timeout": "Engine has not acknowledged Stop; microphone and new playback remain blocked.",
}


def native_factory(engine: str) -> TTSEngine:
    from sofia.tts.native import NativeEngine

    return NativeEngine(engine)


class TTSController(QObject):
    changed = Signal()
    idle = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        factory: Callable[[str], TTSEngine] = native_factory,
    ) -> None:
        super().__init__(parent)
        self.factory = factory
        self.config = TTSConfig()
        self.catalog: dict[str, Capabilities] = {}
        self.queue = PlaybackQueue()
        self.state = "idle"
        self.notice = ""
        self._engine: TTSEngine | None = None
        self._blocked = False
        self._closing = False
        self._cancelled = False
        self._failure = ""
        self._begin = 0.0
        self._stop_begin = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    @property
    def busy(self) -> bool:
        return self._engine is not None or self.queue.active is not None

    def _set(self, state: str) -> None:
        state = state if state in MESSAGES else "playback_failed"
        if self.busy and state in ERRORS and state != "stop_timeout":
            self.notice = state
        else:
            self.state = state
            self.notice = ""
        self.changed.emit()

    def discover(self) -> None:
        # Invoked by Settings only. Startup neither constructs nor speaks a voice.
        from sofia.tts.native import available_engines

        if self.busy or self._closing:
            return
        catalog = {}
        for name in available_engines():
            engine = None
            try:
                engine = self.factory(name)
                caps = engine.capabilities
                if not caps.network_required:
                    catalog[name] = caps
            except Exception:
                continue
            finally:
                if engine:
                    engine.close()
        self.catalog = catalog
        if not catalog:
            self._set("unavailable")

    def configure(self, config: TTSConfig) -> bool:
        try:
            if type(config) is not TTSConfig:
                raise TTSError("invalid_settings")
            if config.enabled:
                if config.engine not in self.catalog:
                    raise TTSError("unknown_engine")
                self.catalog[config.engine].validate(config)
        except TTSError as exc:
            self._set(exc.code)
            return False
        # Existing queued items retain their approved settings. Disable cancels all.
        self.config = config
        if not config.enabled:
            self.cancel()
        self.changed.emit()
        return True

    def speak(self, text: str, *, replace_pending: bool = False) -> bool:
        try:
            if not self.config.enabled:
                raise TTSError("disabled")
            if self._blocked or self._closing or self._cancelled:
                raise TTSError("blocked")
            caps = self.catalog.get(self.config.engine)
            if caps is None:
                raise TTSError("unknown_engine")
            caps.validate(self.config)
            request = TTSRequest(text, self.config)
            logging.getLogger("sofia.tts").info("tts_requested")
            if replace_pending:
                self.queue.replace_pending(request)
            else:
                self.queue.enqueue(request)
        except TTSError as exc:
            self._set(exc.code)
            return False
        logging.getLogger("sofia.tts").info("tts_queued")
        if not self.busy:
            self._set("queued")
        else:
            self.changed.emit()
        # Defer start so Stop can invalidate queued work before native dispatch.
        QTimer.singleShot(0, self._advance)
        return True

    def _advance(self) -> None:
        if self.busy or self._blocked or self._closing:
            return
        request = self.queue.start_next()
        if request is None:
            return
        self._cancelled = False
        self._failure = ""
        self._begin = monotonic()
        self._set("preparing")
        try:
            engine = self.factory(request.config.engine)
            self._engine = engine
            self._timer.start()
            engine.start(request, lambda event: self._event(request.id, engine, event))
        except Exception as exc:
            code = exc.code if isinstance(exc, TTSError) else "playback_failed"
            if self._engine:
                # start() must leave output quiescent when raising.
                self._finish(code)
            else:
                self.queue.complete(request.id)
                self._set(code)
                QTimer.singleShot(0, self._advance)

    def _event(self, request_id: str, engine: TTSEngine, event: str) -> None:
        if (
            self._engine is not engine
            or self.queue.active is None
            or self.queue.active.id != request_id
        ):
            return
        if event in ("completed", "cancelled") or event in ERRORS:
            self._finish(self._failure or ("cancelled" if self._cancelled else event))
        elif not self._cancelled and event in ("preparing", "synthesizing", "playing"):
            self._set(event)
            if event == "playing":
                logging.getLogger("sofia.tts").info("tts_started")

    def _finish(self, event: str) -> None:
        engine, self._engine = self._engine, None
        if self.queue.active:
            self.queue.complete(self.queue.active.id)
        self._timer.stop()
        self._cancelled = False
        if engine:
            engine.close()
        self._set(event)
        audit = (
            "tts_" + event
            if event in ("completed", "cancelled", "unavailable")
            else "tts_failed"
        )
        logging.getLogger("sofia.tts").info(audit)
        self.idle.emit()
        QTimer.singleShot(0, self._advance)

    @Slot()
    def clear_pending(self) -> None:
        self.queue.clear()
        if not self.busy:
            self._set("idle")
        else:
            self.changed.emit()

    @Slot()
    def stop_current(self) -> None:
        if self._engine is not None and not self._cancelled:
            self._cancelled = True
            self._stop_begin = monotonic()
            self._set("stopping")
            try:
                self._engine.stop()
            except Exception:
                # Retain ownership and block new output until acknowledgement.
                self._set("stop_timeout")

    @Slot()
    def cancel(self) -> None:
        active = self.busy or bool(self.queue.pending)
        self.queue.clear()
        self.stop_current()
        if active and not self.busy:
            self._set("cancelled")
        self.changed.emit()

    def block(self, blocked: bool) -> None:
        self._blocked = blocked
        if blocked:
            self.cancel()

    def _tick(self) -> None:
        if self._cancelled:
            if monotonic() - self._stop_begin >= 5:
                self._set("stop_timeout")
        elif monotonic() - self._begin >= 120:
            self._failure = "timeout"
            self.queue.clear()
            self.stop_current()

    def shutdown(self) -> None:
        self._closing = True
        self.cancel()
