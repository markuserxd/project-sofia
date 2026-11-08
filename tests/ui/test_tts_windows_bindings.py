"""Synthetic Windows projection contracts; imports no native Windows package."""

import io
import wave
from threading import Event
from types import SimpleNamespace

import pytest

from sofia.tts import native
from sofia.tts.models import TTSConfig, TTSError, TTSRequest


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 1600)
    return out.getvalue()


@pytest.fixture
def bindings(monkeypatch):
    data = wav_bytes()
    closed = []
    voices = [
        SimpleNamespace(
            id="os-en",
            display_name="English",
            language="en-US",
            gender=SimpleNamespace(name="FEMALE"),
        ),
        SimpleNamespace(
            id="os-th",
            display_name="Thai",
            language="th-TH",
            gender=SimpleNamespace(name="MALE"),
        ),
    ]

    class Operation:
        status = 1

        def __init__(self, value):
            self.value = value

        def get_results(self):
            return self.value

    class Stream:
        size = len(data)

        def get_input_stream_at(self, offset):
            assert offset == 0
            return self

        def close(self):
            closed.append("stream")

    class Synth:
        all_voices = voices

        def __init__(self):
            self.options = SimpleNamespace(speaking_rate=1)

        def synthesize_text_to_stream_async(self, text):
            assert text == "Synthetic sentence."
            return Operation(Stream())

        def close(self):
            closed.append("synth")

    class Reader:
        def __init__(self, stream):
            pass

        def load_async(self, size):
            return Operation(size)

        def read_bytes(self, target):
            target[:] = data

        def close(self):
            closed.append("reader")

    runtime = SimpleNamespace(
        ApartmentType=SimpleNamespace(MULTI_THREADED=0),
        init_apartment=lambda kind: closed.append("init"),
        uninit_apartment=lambda: closed.append("uninit"),
    )
    monkeypatch.setattr(
        native,
        "_bindings",
        lambda: (
            runtime,
            SimpleNamespace(SpeechSynthesizer=Synth),
            SimpleNamespace(DataReader=Reader),
        ),
    )
    return voices, Stream, closed


def test_discovery_rate_gender_and_local_pcm(bindings):
    voices, stream, closed = bindings
    caps = native.discover()
    assert caps.languages == ("en-US", "th-TH")
    assert caps.voices[0].gender == "Female" and caps.voices[1].gender == "Male"
    assert caps.rate_range == (-0.25, 0.25) and not caps.network_required
    request = TTSRequest(
        "Synthetic sentence.",
        TTSConfig(True, native.ENGINE, "en-US", caps.voices[0].id, -0.2),
    )
    pcm = native.synthesize(request, Event())
    assert len(pcm.data) == 3200 and pcm.rate == 16000
    assert closed[-4:] == ["reader", "stream", "synth", "uninit"]


def test_output_bound_checked_before_allocating(bindings):
    _, stream, closed = bindings
    stream.size = 12_000_001
    voice = native.discover().voices[0]
    with pytest.raises(TTSError, match="playback_failed"):
        native.synthesize(
            TTSRequest(
                "Synthetic sentence.", TTSConfig(True, native.ENGINE, "en-US", voice.id)
            ),
            Event(),
        )
    assert "reader" not in closed and closed[-1] == "uninit"


def test_missing_voice_and_pre_cancel_are_safe(bindings):
    _, _, closed = bindings
    request = TTSRequest(
        "Synthetic sentence.", TTSConfig(True, native.ENGINE, "en-US", "missing")
    )
    with pytest.raises(TTSError, match="missing_voice"):
        native.synthesize(request, Event())
    cancel = Event()
    cancel.set()
    with pytest.raises(TTSError):
        native.synthesize(request, cancel)
    assert closed[-1] == "uninit"


def test_native_operation_cancel_waits_for_terminal_status():
    cancel = Event()
    cancel.set()

    class Operation:
        status = 0

        def cancel(self):
            self.status = 2

        def get_results(self):
            pytest.fail("Cancelled result must never be accepted")

    operation = Operation()
    with pytest.raises(TTSError):
        native._wait(operation, cancel)
    assert operation.status == 2
