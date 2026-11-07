"""Deterministic offline provider with explicit failure simulation."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from sofia.providers.base import (
    InvalidRequestError,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderError,
    TemporaryProviderError,
)

MockFailure = Literal["general", "temporary", "invalid_request"]


@dataclass(frozen=True, slots=True)
class MockLLMProvider:
    """Return the same canned text for every valid conversation.

    A configured failure always makes generate() raise. In stream(), it raises
    on iteration after stream_fail_after chunks, or after all available chunks
    if fewer exist. The default fails before the first chunk. Calls share no
    progress state, so consuming one stream never changes another.
    """

    response_text: str = "Hello from SOFIA"
    chunk_size: int = 6
    failure: MockFailure | None = None
    stream_fail_after: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.response_text, str):
            raise ValueError("Mock response_text must be text.")
        if type(self.chunk_size) is not int or self.chunk_size < 1:
            raise ValueError("Mock chunk_size must be a positive integer.")
        if self.failure is not None and (
            not isinstance(self.failure, str)
            or self.failure not in ("general", "temporary", "invalid_request")
        ):
            raise ValueError(
                "Mock failure must be general, temporary, or invalid_request."
            )
        if type(self.stream_fail_after) is not int or self.stream_fail_after < 0:
            raise ValueError("Mock stream_fail_after must be a non-negative integer.")

    def _raise_failure(self) -> None:
        if self.failure == "general":
            raise ProviderError("Simulated mock provider failure.")
        if self.failure == "temporary":
            raise TemporaryProviderError("Simulated temporary mock provider failure.")
        if self.failure == "invalid_request":
            raise InvalidRequestError("Simulated unsupported mock provider request.")

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Validate the request and return canned text or a configured failure."""
        if not isinstance(request, LLMRequest):
            raise InvalidRequestError("Expected an LLMRequest object.")
        self._raise_failure()
        return LLMResponse(content=self.response_text)

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        """Validate and raise failures only when the iterator is advanced."""
        if not isinstance(request, LLMRequest):
            raise InvalidRequestError("Expected an LLMRequest object.")
        for index, start in enumerate(
            range(0, len(self.response_text), self.chunk_size)
        ):
            if index == self.stream_fail_after:
                self._raise_failure()
            yield LLMStreamChunk(
                content=self.response_text[start : start + self.chunk_size]
            )
        self._raise_failure()
