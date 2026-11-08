"""Small immutable speech contracts; text is private, bounded and never executable."""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from uuid import uuid4

MAX_TEXT = 1000
MAX_PENDING = 3
MAX_TOTAL_TEXT = 3000  # Includes the active utterance.

ERRORS = frozenset(
    {
        "invalid_text",
        "unsupported_language",
        "mixed_language",
        "invalid_settings",
        "unknown_engine",
        "missing_voice",
        "unsupported_pitch",
        "unsupported_rate",
        "unavailable",
        "playback_failed",
        "queue_full",
        "disabled",
        "blocked",
        "timeout",
        "stop_timeout",
    }
)


class TTSError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERRORS else "playback_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    name: str
    language: str
    engine: str
    gender: str = "Unknown"

    @property
    def label(self) -> str:
        return f"{self.name} · {self.language} · {self.engine} · {self.gender}"


@dataclass(frozen=True, slots=True)
class Capabilities:
    engine: str
    voices: tuple[Voice, ...]
    rate_range: tuple[float, float] | None = None
    pitch_range: tuple[float, float] | None = None
    rate_description: str = "Engine rate; 0 is normal"
    playback: str = "engine-managed"
    incremental: bool = False
    interruptible: bool = True
    network_required: bool = False
    installed: bool = True

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(sorted({v.language for v in self.voices}))

    def validate(self, config: "TTSConfig") -> Voice:
        if config.engine != self.engine or self.network_required:
            raise TTSError("unknown_engine")
        if not self.installed:
            raise TTSError("unavailable")
        if config.language not in self.languages:
            raise TTSError("unsupported_language")
        voice = next((v for v in self.voices if v.id == config.voice), None)
        if voice is None or voice.language != config.language:
            raise TTSError("missing_voice")
        if self.rate_range is None:
            if config.rate != 0:
                raise TTSError("unsupported_rate")
        elif not self.rate_range[0] <= config.rate <= self.rate_range[1]:
            raise TTSError("unsupported_rate")
        if config.pitch is not None and (
            self.pitch_range is None
            or not self.pitch_range[0] <= config.pitch <= self.pitch_range[1]
        ):
            raise TTSError("unsupported_pitch")
        return voice


@dataclass(frozen=True, slots=True)
class TTSConfig:
    enabled: bool = False
    engine: str = ""
    language: str = "en-US"
    voice: str = ""
    rate: float = 0.0
    volume: float = 0.7
    pitch: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.enabled) is not bool
            or any(type(v) is not str for v in (self.engine, self.language, self.voice))
            or any(len(v) > 256 for v in (self.engine, self.language, self.voice))
            or not _number(self.rate, -1, 1)
            or not _number(self.volume, 0, 1)
            or (self.pitch is not None and not _number(self.pitch, -1, 1))
        ):
            raise TTSError("invalid_settings")


def _number(value: object, low: float, high: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def validate_text(text: str, language: str) -> str:
    if type(text) is not str or not text.strip() or len(text) > MAX_TEXT:
        raise TTSError("invalid_text")
    if language != "th-TH" and not re.fullmatch(r"en-[A-Z]{2}", language):
        raise TTSError("unsupported_language")
    if (
        any(
            unicodedata.category(c) in ("Cs", "Co", "Cn")
            or (unicodedata.category(c).startswith("C") and c not in "\n\t")
            for c in text
        )
        or "<" in text
        or ">" in text
    ):
        # SAPI auto-detects XML. No user markup/SSML is passed to native engines.
        raise TTSError("invalid_text")
    if re.match(r"^(?:[a-zA-Z][\w+.-]*://|[a-zA-Z]:[\\/]|\\\\|/)", text.strip()):
        raise TTSError("invalid_text")
    thai = any("\u0e00" <= c <= "\u0e7f" and c.isalpha() for c in text)
    latin = any(c.isalpha() and "LATIN" in unicodedata.name(c, "") for c in text)
    if thai and latin:
        raise TTSError("mixed_language")
    for c in text:
        if c.isalpha() and not (
            (language == "th-TH" and "\u0e00" <= c <= "\u0e7f")
            or (language.startswith("en-") and "LATIN" in unicodedata.name(c, ""))
        ):
            raise TTSError("unsupported_language")
    return text


@dataclass(frozen=True, slots=True, repr=False)
class TTSRequest:
    text: str
    config: TTSConfig
    id: str = field(default_factory=lambda: uuid4().hex, init=False)

    def __post_init__(self) -> None:
        if type(self.config) is not TTSConfig:
            raise TTSError("invalid_settings")
        validate_text(self.text, self.config.language)
