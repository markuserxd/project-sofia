"""Qt pull-mode microphone capture. All ownership and reads stay on the Qt thread."""

from time import monotonic

from PySide6.QtCore import (
    QCoreApplication,
    QIODevice,
    QMicrophonePermission,
    QObject,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices, QtAudio

from sofia.speech.audio import (
    MAX_SECONDS,
    AudioBuffer,
    AudioFormat,
    Recording,
    SpeechError,
)


class AudioCapture(QObject):
    started = Signal()
    availability_changed = Signal(bool)
    failed = Signal(str)
    limit_reached = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source: QAudioSource | None = None
        self._io: QIODevice | None = None
        self._buffer: AudioBuffer | None = None
        self._started = False
        self._draining = False
        self._begin = 0.0
        self._device_id = b""
        self._devices = QMediaDevices(self)
        self._devices.audioInputsChanged.connect(self._devices_changed)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    def available(self) -> bool:
        return not QMediaDevices.defaultAudioInput().isNull()

    def start(self) -> None:
        if self._source is not None:
            return
        try:
            app = QCoreApplication.instance()
            if (
                app
                and app.checkPermission(QMicrophonePermission())
                == Qt.PermissionStatus.Denied
            ):
                raise SpeechError("permission_denied")
            device = QMediaDevices.defaultAudioInput()
            if device.isNull():
                raise SpeechError("unavailable")
            fmt = QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            if not device.isFormatSupported(fmt):
                fmt = device.preferredFormat()
            sample = {
                QAudioFormat.SampleFormat.UInt8: "u8",
                QAudioFormat.SampleFormat.Int16: "i16",
                QAudioFormat.SampleFormat.Int32: "i32",
                QAudioFormat.SampleFormat.Float: "f32",
            }.get(fmt.sampleFormat(), "unsupported")
            format = AudioFormat(fmt.sampleRate(), fmt.channelCount(), sample)
            self._buffer = AudioBuffer(format)
            self._device_id = bytes(device.id().data())
            self._begin = monotonic()
            source = QAudioSource(device, fmt, self)
            self._source = source
            source.setBufferSize(format.rate * format.frame_bytes // 4)
            source.stateChanged.connect(self._state)
            self._io = source.start()
            # start() can synchronously emit a failure and release the source.
            if self._source is not source:
                self._io = None
                return
            if self._io is None or source.error() != QtAudio.Error.NoError:
                raise SpeechError("capture_failed")
            self._io.readyRead.connect(self._read)
            self._timer.start()
            self._state(source.state())
        except SpeechError as exc:
            self._fail(exc.code)
        except Exception:
            self._fail("capture_failed")

    def _state(self, state: QtAudio.State) -> None:
        source = self._source
        if source is None or (
            self.sender() is not None and self.sender() is not source
        ):
            return
        error = source.error()
        if error != QtAudio.Error.NoError or state in (
            QtAudio.State.StoppedState,
            QtAudio.State.SuspendedState,
        ):
            self._fail("capture_failed")
        elif state == QtAudio.State.ActiveState and not self._started:
            self._started = True
            self.started.emit()

    def _devices_changed(self) -> None:
        self.availability_changed.emit(self.available())
        if self._source and self._device_id not in {
            bytes(d.id().data()) for d in QMediaDevices.audioInputs()
        }:
            self._fail("disconnected")

    def _tick(self) -> None:
        if monotonic() - self._begin >= MAX_SECONDS:
            self.limit_reached.emit()
        elif not self._started and monotonic() - self._begin >= 3:
            self._fail("capture_failed")

    def _read(self) -> None:
        io, buffer = self._io, self._buffer
        if io is None or buffer is None:
            return
        if self.sender() is not None and self.sender() is not io:
            return
        try:
            count = io.bytesAvailable()
            if count < 0 or count > buffer.maximum - buffer.size:
                raise SpeechError("audio_overflow")
            if count:
                buffer.append(bytes(io.read(count).data()))
            if buffer.size == buffer.maximum and not self._draining:
                self.limit_reached.emit()
        except SpeechError as exc:
            self._fail(exc.code)
        except Exception:
            self._fail("capture_failed")

    def _fail(self, code: str) -> None:
        self.discard()
        self.failed.emit(code)

    def _close(self) -> None:
        self._timer.stop()
        source, self._source = self._source, None
        self._io = None
        if source is not None:
            source.reset()
            source.deleteLater()
        self._started = False
        self._device_id = b""

    def finish(self) -> Recording:
        # Drain delivered bytes before reset invalidates the QIODevice.
        self._draining = True
        try:
            self._read()
        finally:
            self._draining = False
        buffer = self._buffer
        self._close()
        self._buffer = None
        if buffer is None:
            raise SpeechError("capture_failed")
        return buffer.finish()

    def discard(self) -> None:
        self._close()
        if self._buffer is not None:
            self._buffer.clear()
            self._buffer = None
