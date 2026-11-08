from array import array

import numpy as np
import pytest

from sofia.speech.audio import (
    AudioBuffer,
    AudioFormat,
    Recording,
    SpeechError,
    normalize,
    validate_text,
)


@pytest.mark.parametrize("rate", [16000, 44100, 48000, 96000])
@pytest.mark.parametrize("channels", [1, 2])
def test_conversion_preserves_duration_and_frequency(rate, channels):
    x = 0.1 * np.sin(2 * np.pi * 440 * np.arange(rate) / rate)
    pcm = np.repeat(x[:, None], channels, axis=1).astype("<f4").tobytes()
    result = normalize(Recording(pcm, AudioFormat(rate, channels, "f32")))
    assert result.shape == (16000,)
    assert result.dtype == np.float32
    assert abs(np.argmax(abs(np.fft.rfft(result))) - 440) <= 1


@pytest.mark.parametrize(
    "sample,dtype,scale",
    [("u8", "u1", 128), ("i16", "<i2", 32768), ("i32", "<i4", 2147483648)],
)
def test_integer_formats(sample, dtype, scale):
    x = np.full(8000, scale // 4)
    if sample == "u8":
        x += 128
    result = normalize(
        Recording(x.astype(dtype).tobytes(), AudioFormat(16000, 1, sample))
    )
    assert np.allclose(result, 0.25)


@pytest.mark.parametrize(
    "rate,channels,sample",
    [(0, 1, "i16"), (192000, 1, "i16"), (16000, 8, "i16"), (16000, 1, "unknown")],
)
def test_unsupported_formats(rate, channels, sample):
    with pytest.raises(SpeechError, match="unsupported_format"):
        AudioFormat(rate, channels, sample)


def test_bounded_buffer_and_split_frames():
    buffer = AudioBuffer(AudioFormat(16000, 1, "i16"))
    buffer.append(b"x")
    buffer.append(b"x" * 9599)
    assert len(buffer.finish().pcm) == 9600
    assert buffer.size == 0
    buffer.append(b"x" * buffer.maximum)
    with pytest.raises(SpeechError, match="audio_overflow"):
        buffer.append(b"x")
    assert buffer.size == buffer.maximum
    buffer.clear()
    assert buffer.size == 0


@pytest.mark.parametrize(
    "pcm,code",
    [
        (b"x", "invalid_audio"),
        (b"\0" * 100, "too_short"),
        (b"\0" * 16000, "no_speech"),
        (b"x" * 480002, "audio_overflow"),
    ],
)
def test_invalid_audio(pcm, code):
    with pytest.raises(SpeechError, match=code):
        normalize(Recording(pcm, AudioFormat(16000, 1, "i16")))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.5])
def test_invalid_float(value):
    with pytest.raises(SpeechError, match="invalid_audio"):
        normalize(
            Recording(
                array("f", [value] * 8000).tobytes(), AudioFormat(16000, 1, "f32")
            )
        )


@pytest.mark.parametrize("text", [None, b"hi", "x" * 4097, "\ud800", "x\0y", " "])
def test_invalid_transcript(text):
    with pytest.raises(SpeechError):
        validate_text(text)


def test_bilingual_text_is_conservatively_normalized():
    assert validate_text("  สวัสดี Project Sofia!\n ") == "สวัสดี Project Sofia!"
