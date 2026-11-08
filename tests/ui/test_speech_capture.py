"""Exercise the actual Qt capture adapter using synthetic QIODevice bytes."""

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, Signal
from PySide6.QtMultimedia import QAudioFormat, QtAudio
from speech_helpers import recording

from sofia.speech import capture as module


@pytest.fixture
def capture(qapp, monkeypatch):
    class Device:
        missing = False
        native = False

        def isNull(self):
            return self.missing

        def id(self):
            return QByteArray(b"synthetic")

        def isFormatSupported(self, fmt):
            return not self.native

        def preferredFormat(self):
            fmt = QAudioFormat()
            fmt.setSampleRate(48000)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            return fmt

    device = Device()

    class Devices(QObject):
        audioInputsChanged = Signal()

        @staticmethod
        def defaultAudioInput():
            return device

        @staticmethod
        def audioInputs():
            return [] if device.missing else [device]

    sources = []

    class Source(QObject):
        stateChanged = Signal(object)

        def __init__(self, device, fmt, parent):
            super().__init__(parent)
            self.io = QBuffer(self)
            self.io.setData(QByteArray(recording().pcm))
            self.io.open(QIODevice.OpenModeFlag.ReadOnly)
            self.closed = False
            self.code = QtAudio.Error.NoError
            sources.append(self)

        def setBufferSize(self, size):
            self.size = size

        def start(self):
            return self.io

        def state(self):
            return QtAudio.State.ActiveState

        def error(self):
            return self.code

        def reset(self):
            self.closed = True
            self.io.close()

    class App:
        permission = Qt.PermissionStatus.Granted

        def checkPermission(self, permission):
            return self.permission

    app = App()
    monkeypatch.setattr(module, "QMediaDevices", Devices)
    monkeypatch.setattr(module, "QAudioSource", Source)
    monkeypatch.setattr(
        module,
        "QCoreApplication",
        type("Application", (), {"instance": staticmethod(lambda: app)}),
    )
    microphone = module.AudioCapture()
    yield microphone, device, sources, app
    microphone.discard()


def test_no_open_at_startup_capture_and_release(capture):
    microphone, _, sources, _ = capture
    assert microphone.available() and not sources
    active = []
    microphone.started.connect(lambda: active.append(True))
    microphone.start()
    microphone.start()
    assert len(sources) == 1 and active == [True]
    audio = microphone.finish()
    assert audio.pcm == recording().pcm
    assert sources[0].closed
    assert microphone._buffer is None and microphone._io is None


@pytest.mark.parametrize(
    "reason",
    [
        "unavailable",
        "permission_denied",
        "disconnected",
        "capture_failed",
        "audio_overflow",
    ],
)
def test_real_adapter_failure_cleanup(capture, reason):
    microphone, device, sources, app = capture
    errors = []
    microphone.failed.connect(errors.append)
    if reason == "unavailable":
        device.missing = True
    elif reason == "permission_denied":
        app.permission = Qt.PermissionStatus.Denied
    microphone.start()
    if reason == "disconnected":
        device.missing = True
        microphone._devices.audioInputsChanged.emit()
    elif reason == "capture_failed":
        sources[0].code = QtAudio.Error.IOError
        sources[0].stateChanged.emit(QtAudio.State.StoppedState)
    elif reason == "audio_overflow":
        sources[0].io.close()
        sources[0].io.setData(QByteArray(b"x" * 480001))
        sources[0].io.open(QIODevice.OpenModeFlag.ReadOnly)
        microphone._read()
    assert errors == [reason]
    assert microphone._buffer is None
    assert all(s.closed for s in sources)


def test_native_format_fallback_and_wall_time_limit(capture):
    microphone, device, _, _ = capture
    device.native = True
    microphone.start()
    assert microphone._buffer.format.rate == 48000
    assert microphone._buffer.format.channels == 2
    limits = []
    microphone.limit_reached.connect(lambda: limits.append(True))
    microphone._begin -= 16
    microphone._tick()
    assert limits == [True]


def test_sample_limit_stops_and_finishes_without_recursive_signal(capture):
    microphone, _, sources, _ = capture
    microphone.start()
    sources[0].io.close()
    sources[0].io.setData(QByteArray(b"x" * 480000))
    sources[0].io.open(QIODevice.OpenModeFlag.ReadOnly)
    results = []
    microphone.limit_reached.connect(lambda: results.append(microphone.finish()))
    microphone._read()
    assert len(results) == 1 and len(results[0].pcm) == 480000
    assert sources[0].closed


def test_delayed_old_device_signals_do_not_affect_new_capture(capture):
    microphone, _, sources, _ = capture
    microphone.start()
    old = sources[0]
    microphone.discard()
    microphone.start()
    old.code = QtAudio.Error.IOError
    old.stateChanged.emit(QtAudio.State.StoppedState)
    assert microphone._source is sources[1]
