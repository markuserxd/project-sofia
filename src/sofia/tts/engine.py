"""Trusted application factories supply engines; providers cannot register them."""

from collections.abc import Callable
from typing import Protocol

from sofia.tts.models import Capabilities, TTSRequest

# Events: preparing, synthesizing, playing, completed, cancelled, or a fixed error.
# Terminal events acknowledge that this instance can no longer produce output.
PlaybackCallback = Callable[[str], None]


class TTSEngine(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    def start(self, request: TTSRequest, callback: PlaybackCallback) -> None:
        """Play one request asynchronously on the engine's owner thread.

        Raise only before dispatch, with no remaining output/work. After dispatch,
        deliver sanitized events on the owner thread instead of raising. Terminal
        events acknowledge quiescence; close must then be safe and nonblocking.
        """
        ...

    def stop(self) -> None:
        """Invalidate work, stop output, then acknowledge cancelled via callback."""
        ...

    def close(self) -> None:
        """Release an idle/acknowledged instance; never destroy active workers."""
        ...
