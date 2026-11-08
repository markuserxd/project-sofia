import io
import wave

import pytest

from sofia.tts.models import TTSError
from sofia.tts.pcm import PCM, decode_wav


@pytest.mark.parametrize(
    "data,rate,channels",
    [
        (b"", 16000, 1),
        (b"x", 16000, 1),
        (b"xx", 7999, 1),
        (b"xxxx", 48001, 1),
        (b"xxxx", 16000, 3),
        (b"xx", 16000, 2),
        ([0, 0], 16000, 1),
        (b"xx", True, 1),
        (b"\x00\x00" * (8000 * 60 + 1), 8000, 1),
    ],
)
def test_invalid_pcm(data, rate, channels):
    with pytest.raises(TTSError, match="playback_failed"):
        PCM(data, rate, channels)


def test_wav_memory_only_and_truncation():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 1600)
    encoded = out.getvalue()
    pcm = decode_wav(encoded)
    assert pcm.rate == 16000 and pcm.channels == 1 and len(pcm.data) == 3200
    assert "data" not in repr(pcm)
    for value in (encoded[:-1], b"not WAV", b"x" * 12_000_001):
        with pytest.raises(TTSError, match="playback_failed"):
            decode_wav(value)
