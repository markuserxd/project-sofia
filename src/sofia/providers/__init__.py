"""Text provider contracts and the offline mock implementation."""

from sofia.providers.base import (
    InvalidRequestError,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    ProviderError,
    TemporaryProviderError,
)
from sofia.providers.mock import MockFailure, MockLLMProvider

__all__ = [
    "InvalidRequestError",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamChunk",
    "MessageRole",
    "MockFailure",
    "MockLLMProvider",
    "ProviderError",
    "TemporaryProviderError",
]
