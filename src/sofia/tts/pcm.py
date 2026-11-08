"""Bounded integer PCM only. No file paths, floats, resampling or relabeling."""

import io
import wave
from dataclasses import dataclass

from sofia.tts.models import TTSError

MAX_AUDIO_BYTES = 12_000_000
MAX_AUDIO_SECONDS = 60


@dataclass(frozen=True, slots=True, repr=False)
class PCM:
    data: bytes
    rate: int
    channels: int

    def __post_init__(self) -> None:
        if (
            type(self.data) is not bytes
            or not self.data
            or type(self.rate) is not int
            or not 8000 <= self.rate <= 48000
            or type(self.channels) is not int
            or self.channels not in (1, 2)
            or len(self.data) % (2 * self.channels)
            or len(self.data) > MAX_AUDIO_BYTES
            or len(self.data) > self.rate * self.channels * 2 * MAX_AUDIO_SECONDS
        ):
            raise TTSError("playback_failed")


def decode_wav(data: bytes) -> PCM:
    if type(data) is not bytes or len(data) > MAX_AUDIO_BYTES:
        raise TTSError("playback_failed")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise TTSError("playback_failed")
            rate, channels, frames = (
                wav.getframerate(),
                wav.getnchannels(),
                wav.getnframes(),
            )
            if (
                frames > rate * MAX_AUDIO_SECONDS
                or frames * channels * 2 > MAX_AUDIO_BYTES
            ):
                raise TTSError("playback_failed")
            pcm = wav.readframes(frames + 1)
            if len(pcm) != frames * channels * 2:
                raise TTSError("playback_failed")
            return PCM(pcm, rate, channels)
    except Exception:
        raise TTSError("playback_failed") from None
