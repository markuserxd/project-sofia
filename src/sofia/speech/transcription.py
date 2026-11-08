"""Local directory only: no model identifiers, downloads, uploads or task APIs."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, Protocol

from sofia.speech.audio import Recording, SpeechError, normalize, validate_text


@dataclass(frozen=True, slots=True, repr=False)
class Transcription:
    text: str = field(repr=False)
    language: str


class Recognizer(Protocol):
    def transcribe(
        self, audio: Recording, language: str, cancel: Event
    ) -> Transcription: ...


class OfflineRecognizer:
    def __init__(self, model_path: Path | None) -> None:
        self._path = model_path
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        path = self._path
        if (
            path is None
            or not path.is_absolute()
            or str(path).startswith(("\\\\", "//"))
        ):
            raise SpeechError("missing_model")
        path = path.resolve(strict=True)
        if str(path).startswith(("\\\\", "//")):
            raise SpeechError("missing_model")
        # tokenizer.json is mandatory: faster-whisper otherwise fetches a tokenizer.
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            file = path / name
            if (
                not file.is_file()
                or file.resolve().parent != path
                or file.stat().st_size == 0
            ):
                raise SpeechError("missing_model")
        import onnxruntime  # type: ignore[import-untyped]
        from faster_whisper import WhisperModel  # type: ignore[import-untyped,import-not-found]

        onnxruntime.disable_telemetry_events()

        # Upstream DEBUG diagnostics can contain decoded speech. Suppress them
        # before construction and inference, regardless of SOFIA's logging level.
        logging.getLogger("faster_whisper").disabled = True
        model = WhisperModel(
            str(path),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
            cpu_threads=4,
            num_workers=1,
        )
        if not model.model.is_multilingual or not {"th", "en"}.issubset(
            model.supported_languages
        ):
            raise SpeechError("unsupported_model")
        self._model = model
        return model

    def transcribe(
        self, audio: Recording, language: str, cancel: Event
    ) -> Transcription:
        deadline = monotonic() + 120

        def checkpoint() -> None:
            if cancel.is_set():
                raise SpeechError("cancelled")
            if monotonic() >= deadline:
                raise SpeechError("timeout")

        if language not in ("th", "en"):
            raise SpeechError("unsupported_language")
        checkpoint()
        try:
            samples = normalize(audio)
            checkpoint()
            model = self._load()
            checkpoint()
            segments, _ = model.transcribe(
                samples,
                language=language,
                task="transcribe",
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                max_new_tokens=440,
                log_progress=False,
            )
            text = ""
            for index, segment in enumerate(segments):
                checkpoint()
                if index >= 128 or type(segment.text) is not str:
                    raise SpeechError("invalid_transcript")
                text += segment.text
                if len(text) > 4096:
                    raise SpeechError("invalid_transcript")
            checkpoint()
            return Transcription(validate_text(text), language)
        except SpeechError:
            raise
        except FileNotFoundError:
            raise SpeechError("missing_model") from None
        except ImportError:
            raise SpeechError("missing_backend") from None
        except Exception:
            raise SpeechError("transcription_failed") from None
