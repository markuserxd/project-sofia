"""Installed Windows voices synthesize offline; SOFIA owns bounded PCM playback."""

import importlib
import sys
from hashlib import sha256
from threading import Event
from time import sleep
from typing import Any

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QCoreApplication,
    QIODevice,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QtAudio

from sofia.tts.engine import PlaybackCallback
from sofia.tts.models import (
    Capabilities,
    TTSConfig,
    TTSError,
    TTSRequest,
    Voice,
    validate_text,
)
from sofia.tts.pcm import MAX_AUDIO_BYTES, PCM, decode_wav

ENGINE = "windows-local"


def _bindings() -> tuple[Any, Any, Any]:
    runtime = importlib.import_module("winrt.runtime")
    speech = importlib.import_module("winrt.windows.media.speechsynthesis")
    streams = importlib.import_module("winrt.windows.storage.streams")
    importlib.import_module("winrt.windows.foundation.collections")
    return runtime, speech, streams


def available_engines() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    try:
        _bindings()
    except (ImportError, OSError):
        return ()
    return (ENGINE,)


def _voice_id(voice: Any) -> str:
    return sha256(voice.id.encode("utf-8")).hexdigest()


def discover() -> Capabilities:
    _, speech, _ = _bindings()
    voices = []
    for voice in speech.SpeechSynthesizer.all_voices:
        language = voice.language
        if language != "th-TH" and not language.startswith("en-"):
            continue
        gender = voice.gender.name.title()
        voices.append(
            Voice(
                _voice_id(voice),
                voice.display_name,
                language,
                ENGINE,
                gender if gender in ("Male", "Female") else "Unknown",
            )
        )
    probe = speech.SpeechSynthesizer()
    try:
        try:
            probe.options.speaking_rate
            rate_range = (-0.25, 0.25)
        except (AttributeError, OSError):
            rate_range = None
    finally:
        probe.close()
    return Capabilities(
        ENGINE,
        tuple(voices),
        rate_range,
        rate_description="Speed change in percent: -25 = 0.75x; +25 = 1.25x",
        playback="PCM",
    )


def _wait(operation: Any, cancel: Event) -> Any:
    requested = False
    while int(operation.status) == 0:
        if cancel.is_set() and not requested:
            operation.cancel()
            requested = True
        sleep(0.01)
    if cancel.is_set():
        raise TTSError("blocked")
    return operation.get_results()


def synthesize(request: TTSRequest, cancel: Event) -> PCM:
    runtime, speech, streams = _bindings()
    runtime.init_apartment(runtime.ApartmentType.MULTI_THREADED)
    try:

        def render() -> PCM:
            if cancel.is_set():
                raise TTSError("blocked")
            synth = speech.SpeechSynthesizer()
            stream = reader = None
            try:
                voice = next(
                    (
                        v
                        for v in speech.SpeechSynthesizer.all_voices
                        if _voice_id(v) == request.config.voice
                        and v.language == request.config.language
                    ),
                    None,
                )
                if voice is None:
                    raise TTSError("missing_voice")
                synth.voice = voice
                if request.config.rate:
                    try:
                        speed = 1 + request.config.rate
                        synth.options.speaking_rate = speed
                        if abs(synth.options.speaking_rate - speed) > 0.001:
                            raise TTSError("unsupported_rate")
                    except (AttributeError, OSError):
                        raise TTSError("unsupported_rate") from None
                stream = _wait(
                    synth.synthesize_text_to_stream_async(request.text), cancel
                )
                size = stream.size
                if not 0 < size <= MAX_AUDIO_BYTES:
                    raise TTSError("playback_failed")
                reader = streams.DataReader(stream.get_input_stream_at(0))
                loaded = _wait(reader.load_async(size), cancel)
                if loaded != size:
                    raise TTSError("playback_failed")
                data = bytearray(size)
                reader.read_bytes(data)
                return decode_wav(bytes(data))
            finally:
                if reader is not None:
                    reader.close()
                if stream is not None:
                    stream.close()
                synth.close()

        return render()
    finally:
        runtime.uninit_apartment()


class _Synthesis(QObject):
    result = Signal(str, object, str)
    finished = Signal()

    def __init__(self, request: TTSRequest, cancel: Event) -> None:
        super().__init__()
        self.request: TTSRequest | None = request
        self.session = request.id
        self.cancel = cancel

    @Slot()
    def execute(self) -> None:
        pcm = None
        code = ""
        try:
            assert self.request is not None
            pcm = synthesize(self.request, self.cancel)
        except BaseException as exc:
            code = exc.code if isinstance(exc, TTSError) else "playback_failed"
        finally:
            self.request = None
        if self.cancel.is_set():
            pcm, code = None, "cancelled"
        self.result.emit(self.session, pcm, code)
        self.finished.emit()


class NativeEngine(QObject):
    def __init__(self, engine: str, parent: QObject | None = None) -> None:
        if engine not in available_engines():
            raise TTSError("unknown_engine")
        super().__init__(parent or QCoreApplication.instance())
        self._capabilities = discover()
        self._callback: PlaybackCallback | None = None
        self._thread: QThread | None = None
        self._worker: _Synthesis | None = None
        self._cancel = Event()
        self._received = self._done = False
        self._pcm: PCM | None = None
        self._code = ""
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None
        self._config = TTSConfig()
        self._used = False
        self._played = False
        self._session = ""
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._outputs_changed)
        self._output_id = b""

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def start(self, request: TTSRequest, callback: PlaybackCallback) -> None:
        if self._used:
            raise TTSError("unavailable")
        self.capabilities.validate(request.config)
        validate_text(request.text, request.config.language)
        if not request.config.enabled:
            raise TTSError("disabled")
        if not QMediaDevices.audioOutputs():
            raise TTSError("unavailable")
        self._output_id = bytes(QMediaDevices.defaultAudioOutput().id().data())
        self._used = True
        self._session = request.id
        self._config = request.config
        self._callback = callback
        thread = QThread(self)
        worker = _Synthesis(request, self._cancel)
        self._thread, self._worker = thread, worker
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.result.connect(self._result, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._thread_done, Qt.ConnectionType.QueuedConnection)
        callback("synthesizing")
        thread.start()

    @Slot(str, object, str)
    def _result(self, session: str, pcm: PCM | None, code: str) -> None:
        if session == self._session and self._thread is not None and not self._received:
            self._received, self._pcm = True, pcm
            self._code = self._code or code
            self._finish_synthesis()

    @Slot()
    def _thread_done(self) -> None:
        if self.sender() is self._thread:
            self._done = True
            QTimer.singleShot(0, self._finish_synthesis)

    def _finish_synthesis(self) -> None:
        thread = self._thread
        if thread is None or not self._done or not self._received:
            return
        if not thread.wait(0):
            QTimer.singleShot(5, self._finish_synthesis)
            return
        self._thread = None
        self._worker = None
        thread.deleteLater()
        pcm, self._pcm = self._pcm, None
        if self._cancel.is_set():
            self._terminal(self._code if self._code == "unavailable" else "cancelled")
        elif self._code or pcm is None:
            self._terminal(self._code or "playback_failed")
        else:
            try:
                self._play(pcm)
            except Exception:
                self._terminal("playback_failed")

    def _play(self, pcm: PCM) -> None:
        if type(pcm) is not PCM:
            raise TTSError("playback_failed")
        fmt = QAudioFormat()
        fmt.setSampleRate(pcm.rate)
        fmt.setChannelCount(pcm.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull() or bytes(device.id().data()) != self._output_id:
            self._terminal("unavailable")
            return
        if not device.isFormatSupported(fmt):
            self._terminal("unavailable")
            return
        buffer = QBuffer(self)
        buffer.setData(QByteArray(pcm.data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        sink = QAudioSink(device, fmt, self)
        self._buffer, self._sink = buffer, sink
        sink.setVolume(self._config.volume)
        sink.stateChanged.connect(self._state)
        sink.start(buffer)
        self._observe()

    @Slot()
    def _state(self) -> None:
        if self.sender() is self._sink:
            self._observe()

    def _observe(self) -> None:
        sink = self._sink
        if sink is None or self._cancel.is_set():
            return
        if sink.error() is not QtAudio.Error.NoError:
            self._terminal("playback_failed")
        elif sink.state() is QtAudio.State.ActiveState:
            if not self._played and self._callback:
                self._played = True
                self._callback("playing")
        elif sink.state() is QtAudio.State.IdleState:
            if self._played and self._buffer and self._buffer.atEnd():
                self._terminal("completed")
        elif sink.state() is QtAudio.State.StoppedState and not self._cancel.is_set():
            self._terminal("playback_failed")

    @Slot()
    def _outputs_changed(self) -> None:
        if self._callback and self._output_id not in {
            device.id().data() for device in QMediaDevices.audioOutputs()
        }:
            self._code = "unavailable"
            self.stop()

    def _terminal(self, event: str) -> None:
        callback, self._callback = self._callback, None
        sink, self._sink = self._sink, None
        buffer, self._buffer = self._buffer, None
        if sink:
            sink.reset()
            sink.deleteLater()
        if buffer:
            buffer.close()
            buffer.deleteLater()
        if callback:
            callback(event)

    @Slot()
    def stop(self) -> None:
        self._cancel.set()
        self._pcm = None
        if self._sink:
            self._sink.reset()
        if self._thread is None:
            self._terminal(
                "unavailable" if self._code == "unavailable" else "cancelled"
            )

    def close(self) -> None:
        if self._thread is not None or self._sink is not None:
            raise TTSError("blocked")
        self._used = True
        self.deleteLater()
