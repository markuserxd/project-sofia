"""Own PCM sink and synthesis cancellation tests with no native audio access."""

from threading import Event

import pytest
from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtMultimedia import QtAudio
from tts_helpers import CAPS

from sofia.tts import native
from sofia.tts.models import TTSConfig, TTSError, TTSRequest
from sofia.tts.pcm import PCM


class Devices(QObject):
    audioOutputsChanged = Signal()
    connected = True
    supported = True

    @staticmethod
    def audioOutputs():
        return [Devices()] if Devices.connected else []

    @staticmethod
    def defaultAudioOutput():
        return Devices()

    def id(self):
        return QByteArray(b"test-output")

    def isNull(self):
        return not Devices.connected

    def isFormatSupported(self, fmt):
        return Devices.supported


class Sink(QObject):
    stateChanged = Signal(object)

    def __init__(self, device, fmt, parent):
        super().__init__(parent)
        self.current = QtAudio.State.StoppedState
        self.failure = QtAudio.Error.NoError
        self.resets = 0
        self.format = fmt

    def setVolume(self, value):
        self.volume = value

    def start(self, buffer):
        self.current = QtAudio.State.ActiveState
        self.stateChanged.emit(object())

    def state(self):
        return self.current

    def error(self):
        return self.failure

    def reset(self):
        self.resets += 1
        self.current = QtAudio.State.StoppedState
        self.stateChanged.emit(object())


@pytest.fixture
def adapter(qapp, monkeypatch, wait):
    monkeypatch.setattr(native, "available_engines", lambda: ("fake",))
    monkeypatch.setattr(native, "discover", lambda: CAPS)
    monkeypatch.setattr(native, "QMediaDevices", Devices)
    monkeypatch.setattr(native, "QAudioSink", Sink)
    monkeypatch.setattr(Devices, "connected", True)
    monkeypatch.setattr(Devices, "supported", True)
    monkeypatch.setattr(
        native, "synthesize", lambda req, cancel: PCM(b"\x00\x00" * 1600, 16000, 1)
    )
    engine = native.NativeEngine("fake")
    yield engine
    engine.stop()
    wait(lambda: engine._thread is None)
    engine.close()
    qapp.processEvents()


def start(engine):
    events = []
    engine.start(
        TTSRequest(
            "Synthetic sentence.", TTSConfig(True, "fake", "en-US", "en", volume=0.4)
        ),
        events.append,
    )
    return events


def test_bounded_pcm_real_states_and_native_enum_slot(adapter, wait):
    events = start(adapter)
    assert events == ["synthesizing"]
    wait(lambda: "playing" in events)
    sink = adapter._sink
    assert sink.volume == 0.4 and sink.format.sampleRate() == 16000
    assert sink.format.channelCount() == 1
    # Idle alone is not completion while unread data remains.
    sink.current = QtAudio.State.IdleState
    sink.stateChanged.emit(object())
    assert events[-1] == "playing"
    adapter._buffer.seek(adapter._buffer.size())
    sink.stateChanged.emit(object())
    assert events[-1] == "completed" and sink.resets > 0


def test_stop_discards_sink_and_no_late_completion(adapter, wait):
    events = start(adapter)
    wait(lambda: "playing" in events)
    old_sink = adapter._sink
    adapter.stop()
    assert events[-1] == "cancelled" and old_sink.resets > 0
    assert adapter._sink is None and adapter._buffer is None
    old_sink.current = QtAudio.State.IdleState
    old_sink.stateChanged.emit(object())
    assert events[-1] == "cancelled"


def test_cancel_during_synthesis_retains_worker_until_finished(
    adapter, wait, monkeypatch
):
    entered, release = Event(), Event()

    def slow(req, cancel):
        entered.set()
        release.wait(3)
        return PCM(b"\x00\x00" * 1600, 16000, 1)

    monkeypatch.setattr(native, "synthesize", slow)
    events = start(adapter)
    wait(entered.is_set)
    adapter.stop()
    assert adapter._thread is not None and events == ["synthesizing"]
    with pytest.raises(TTSError, match="blocked"):
        adapter.close()
    release.set()
    wait(lambda: adapter._thread is None)
    assert events[-1] == "cancelled" and "playing" not in events


@pytest.mark.parametrize("failure", ["missing", "format", "synthesis", "device"])
def test_failures_sanitized(adapter, monkeypatch, wait, failure, caplog):
    if failure == "missing":
        monkeypatch.setattr(Devices, "connected", False)
        with pytest.raises(TTSError, match="unavailable"):
            start(adapter)
        return
    if failure == "format":
        monkeypatch.setattr(Devices, "supported", False)
    if failure == "synthesis":

        def fail(req, cancel):
            raise RuntimeError("PRIVATE CONTENT C:/private")

        monkeypatch.setattr(native, "synthesize", fail)
    events = start(adapter)
    wait(lambda: adapter._thread is None)
    if failure == "device":
        monkeypatch.setattr(Devices, "connected", False)
        adapter._devices.audioOutputsChanged.emit()
    assert events[-1] in ("unavailable", "playback_failed")
    assert "PRIVATE" not in caplog.text


def test_output_error_during_playback(adapter, wait):
    events = start(adapter)
    wait(lambda: "playing" in events)
    adapter._sink.failure = QtAudio.Error.IOError
    adapter._sink.stateChanged.emit(object())
    assert events[-1] == "playback_failed"
