import sys
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from sofia.speech.audio import AudioFormat, Recording, SpeechError
from sofia.speech.transcription import OfflineRecognizer


@pytest.fixture
def model(tmp_path, monkeypatch):
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    calls = []
    backend = SimpleNamespace(
        model=SimpleNamespace(is_multilingual=True), supported_languages=["en", "th"]
    )
    backend.transcribe = lambda audio, **kw: (
        calls.append((audio, kw)) or iter([SimpleNamespace(text="สวัสดี Hello!")]),
        None,
    )

    def construct(path, **kw):
        calls.append((path, kw))
        return backend

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=construct)
    )
    return OfflineRecognizer(tmp_path), backend, calls, tmp_path


def audio():
    return Recording(
        np.full(8000, 0.1, dtype="<f4").tobytes(), AudioFormat(16000, 1, "f32")
    )


@pytest.mark.parametrize("language", ["th", "en"])
def test_local_only_multilingual_model_reused(model, language):
    recognizer, _, calls, path = model
    result = recognizer.transcribe(audio(), language, Event())
    assert result.text == "สวัสดี Hello!"
    assert result.language == language
    assert calls[0] == (
        str(path.resolve()),
        {
            "device": "cpu",
            "compute_type": "int8",
            "local_files_only": True,
            "cpu_threads": 4,
            "num_workers": 1,
        },
    )
    assert calls[1][1]["language"] == language
    assert calls[1][1]["task"] == "transcribe"
    assert calls[1][1]["vad_filter"]
    recognizer.transcribe(audio(), language, Event())
    assert len(calls) == 3
    assert "สวัสดี" not in repr(result)


@pytest.mark.parametrize(
    "missing", ["model.bin", "tokenizer.json", "config.json", "vocabulary.txt"]
)
def test_missing_files_never_construct_or_download(model, missing):
    recognizer, _, calls, path = model
    (path / missing).unlink()
    with pytest.raises(SpeechError, match="missing_model"):
        recognizer.transcribe(audio(), "th", Event())
    assert calls == []


@pytest.mark.parametrize(
    "path", [None, "base", "https://example.com/model", "//server/share/model"]
)
def test_no_remote_identifiers(path):
    from pathlib import Path

    with pytest.raises(SpeechError, match="missing_model"):
        OfflineRecognizer(Path(path) if path else None).transcribe(
            audio(), "en", Event()
        )


def test_english_only_model_rejected(model):
    recognizer, backend, _, _ = model
    backend.model.is_multilingual = False
    with pytest.raises(SpeechError, match="unsupported_model"):
        recognizer.transcribe(audio(), "th", Event())


def test_cancellation_before_and_during_model_loading(model, monkeypatch):
    recognizer, _, calls, _ = model
    cancel = Event()
    cancel.set()
    with pytest.raises(SpeechError, match="cancelled"):
        recognizer.transcribe(audio(), "en", cancel)
    assert not calls
    cancel.clear()
    original = recognizer._load

    def load():
        loaded = original()
        cancel.set()
        return loaded

    monkeypatch.setattr(recognizer, "_load", load)
    with pytest.raises(SpeechError, match="cancelled"):
        recognizer.transcribe(audio(), "en", cancel)
    assert len(calls) == 1


def test_cancellation_during_lazy_inference(model):
    recognizer, backend, _, _ = model
    cancel = Event()

    def segments():
        cancel.set()
        yield SimpleNamespace(text="private")

    backend.transcribe = lambda *a, **kw: (segments(), None)
    with pytest.raises(SpeechError, match="cancelled"):
        recognizer.transcribe(audio(), "th", cancel)


@pytest.mark.parametrize(
    "output,code",
    [
        [[], "no_speech"],
        [[None], "invalid_transcript"],
        [["x" * 4097], "invalid_transcript"],
        [["x"] * 129, "invalid_transcript"],
    ],
)
def test_bounded_lazy_output(model, output, code):
    recognizer, backend, _, _ = model
    backend.transcribe = lambda *a, **kw: (
        iter(SimpleNamespace(text=t) for t in output),
        None,
    )
    with pytest.raises(SpeechError, match=code):
        recognizer.transcribe(audio(), "en", Event())


def test_invalid_language_never_loads(model):
    recognizer, _, calls, _ = model
    with pytest.raises(SpeechError, match="unsupported_language"):
        recognizer.transcribe(audio(), "xx", Event())
    assert calls == []


def test_raw_backend_exception_never_exposed_or_logged(model, caplog):
    recognizer, backend, _, _ = model

    def fail(*a, **kw):
        raise RuntimeError("secret speech and private filename")

    backend.transcribe = fail
    with pytest.raises(SpeechError, match="transcription_failed") as exc:
        recognizer.transcribe(audio(), "en", Event())
    assert "secret" not in str(exc.value)
    assert "secret" not in caplog.text
