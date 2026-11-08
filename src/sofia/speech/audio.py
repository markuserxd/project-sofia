"""Bounded PCM storage and worker-only conversion to mono 16 kHz."""

from dataclasses import dataclass, field
from math import gcd
from typing import Any

MAX_SECONDS = 15
MIN_SECONDS = 0.3
MAX_TEXT = 4096


class SpeechError(Exception):
    """Only application-owned codes cross the backend boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AudioFormat:
    rate: int
    channels: int
    sample: str

    def __post_init__(self) -> None:
        if (
            type(self.rate) is not int
            or not 8000 <= self.rate <= 96000
            or type(self.channels) is not int
            or self.channels not in (1, 2)
            or self.sample not in ("u8", "i16", "i32", "f32")
        ):
            raise SpeechError("unsupported_format")

    @property
    def frame_bytes(self) -> int:
        return self.channels * {"u8": 1, "i16": 2, "i32": 4, "f32": 4}[self.sample]


@dataclass(frozen=True, slots=True, repr=False)
class Recording:
    pcm: bytes = field(repr=False)
    format: AudioFormat


class AudioBuffer:
    def __init__(self, format: AudioFormat) -> None:
        self.format = format
        self.maximum = format.rate * MAX_SECONDS * format.frame_bytes
        self._data = bytearray()

    @property
    def size(self) -> int:
        return len(self._data)

    def append(self, data: bytes) -> None:
        # Qt may split a PCM frame across reads. Validate alignment at finish.
        if type(data) is not bytes or len(data) > self.maximum - self.size:
            raise SpeechError("audio_overflow")
        self._data.extend(data)

    def clear(self) -> None:
        self._data.clear()

    def finish(self) -> Recording:
        try:
            recording = Recording(bytes(self._data), self.format)
            validate_recording(recording)
            return recording
        finally:
            self.clear()


def validate_recording(recording: Recording) -> None:
    fmt = recording.format
    if type(recording.pcm) is not bytes or len(recording.pcm) % fmt.frame_bytes:
        raise SpeechError("invalid_audio")
    frames = len(recording.pcm) // fmt.frame_bytes
    if frames > fmt.rate * MAX_SECONDS:
        raise SpeechError("audio_overflow")
    if frames < fmt.rate * MIN_SECONDS:
        raise SpeechError("too_short")


def normalize(recording: Recording) -> Any:
    """Import optional numeric libraries only on the inference worker."""
    import numpy as np
    from scipy.signal import resample_poly  # type: ignore[import-untyped]

    validate_recording(recording)
    fmt = recording.format
    dtype = {"u8": "u1", "i16": "<i2", "i32": "<i4", "f32": "<f4"}[fmt.sample]
    samples: Any = np.frombuffer(recording.pcm, dtype=dtype).astype(np.float32)
    if fmt.sample == "u8":
        samples = (samples - 128) / 128
    elif fmt.sample != "f32":
        samples /= 32768 if fmt.sample == "i16" else 2147483648
    if not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
        raise SpeechError("invalid_audio")
    samples = samples.reshape(-1, fmt.channels).mean(axis=1)
    if fmt.rate != 16000:
        divisor = gcd(fmt.rate, 16000)
        samples = resample_poly(samples, 16000 // divisor, fmt.rate // divisor)
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if (
        not 4800 <= samples.size <= 16000 * MAX_SECONDS
        or not np.isfinite(samples).all()
    ):
        raise SpeechError("invalid_audio")
    # Only reject near-digital silence; VAD handles ordinary quiet speech.
    if float(np.max(np.abs(samples))) < 0.0001:
        raise SpeechError("no_speech")
    return samples


def validate_text(text: object) -> str:
    if type(text) is not str or len(text) > MAX_TEXT:
        raise SpeechError("invalid_transcript")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise SpeechError("invalid_transcript") from None
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise SpeechError("invalid_transcript")
    text = text.strip()
    if not text:
        raise SpeechError("no_speech")
    return text
