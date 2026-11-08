"""Bounded FIFO with one active request; no Qt/audio/provider dependencies."""

from collections import deque

from sofia.tts.models import MAX_PENDING, MAX_TOTAL_TEXT, TTSError, TTSRequest


class PlaybackQueue:
    def __init__(self) -> None:
        self.active: TTSRequest | None = None
        self._pending: deque[TTSRequest] = deque()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def enqueue(self, request: TTSRequest) -> None:
        if type(request) is not TTSRequest:
            raise TTSError("invalid_text")
        items = tuple(self._pending) + ((self.active,) if self.active else ())
        if any(item.id == request.id for item in items):
            raise TTSError("invalid_text")
        if len(self._pending) >= MAX_PENDING or (
            sum(len(item.text) for item in items) + len(request.text) > MAX_TOTAL_TEXT
        ):
            raise TTSError("queue_full")
        self._pending.append(request)

    def start_next(self) -> TTSRequest | None:
        if self.active is None and self._pending:
            self.active = self._pending.popleft()
        return self.active

    def complete(self, request_id: str) -> bool:
        if self.active is None or self.active.id != request_id:
            return False
        self.active = None
        return True

    def clear(self) -> None:
        self._pending.clear()

    def replace_pending(self, request: TTSRequest) -> None:
        # Validate limits before discarding existing pending work.
        replacement = PlaybackQueue()
        replacement.active = self.active
        replacement.enqueue(request)
        self._pending = replacement._pending
