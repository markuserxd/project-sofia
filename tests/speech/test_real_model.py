"""Opt-in consented local PCM samples. The global socket ban still applies."""

import os
import wave
from pathlib import Path
from threading import Event

import pytest

from sofia.speech.audio import AudioFormat, Recording, validate_recording
from sofia.speech.transcription import OfflineRecognizer


@pytest.mark.speech_model
@pytest.mark.parametrize(
    "language,variable", [("th", "SOFIA_STT_THAI_WAV"), ("en", "SOFIA_STT_ENGLISH_WAV")]
)
def test_consented_local_speech(language, variable):
    if os.environ.get("SOFIA_SPEECH_MODEL_TEST") != "1":
        pytest.skip(
            "Opt in with an installed local model and consented local WAV files"
        )
    model = os.environ.get("SOFIA_STT_MODEL_DIR")
    sample = os.environ.get(variable)
    assert model and sample, "Configure model and both consented speech samples"
    with wave.open(sample, "rb") as wav:
        assert wav.getcomptype() == "NONE" and wav.getsampwidth() == 2
        fmt = AudioFormat(wav.getframerate(), wav.getnchannels(), "i16")
        assert 0 < wav.getnframes() <= fmt.rate * 15
        audio = Recording(wav.readframes(wav.getnframes()), fmt)
    validate_recording(audio)
    result = OfflineRecognizer(Path(model)).transcribe(audio, language, Event())
    assert result.text.strip() and result.language == language
    if language == "th":
        assert any("\u0e00" <= char <= "\u0e7f" for char in result.text)
    else:
        assert any(char.isascii() and char.isalpha() for char in result.text)
    # Text is deliberately not printed into test reports or stored as artifacts.
