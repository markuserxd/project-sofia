"""Small synchronous contract for text-only LLM providers."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

MessageRole = Literal["system", "user", "assistant"]


class ProviderError(Exception):
    """Generation failed. Provider implementations must use safe error messages."""


class TemporaryProviderError(ProviderError):
    """A temporary provider failure; no automatic retry is performed."""


class ProviderAccessError(ProviderError):
    """The credential or account lacks access."""


class ProviderFundsError(ProviderError):
    """The provider reported a billing or credit limit."""


class ProviderRateLimitError(TemporaryProviderError):
    """The provider reported a rate limit."""


class ProviderTimeoutError(TemporaryProviderError):
    """The provider or transport timed out."""


class UndispatchedProviderError(TemporaryProviderError):
    """Trusted adapter evidence: no dispatch/output/charge, reservation settled.

    Only transport connection-establishment evidence may produce this signal.
    Generic temporary failures never authorize retry or fallback.
    """

    def __init__(self) -> None:
        super().__init__("provider_connection_not_dispatched")


class InvalidRequestError(ProviderError):
    """The request is malformed or unsupported by the provider."""


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in (
            "system",
            "user",
            "assistant",
        ):
            raise InvalidRequestError(
                "Message role must be system, user, or assistant."
            )
        if not isinstance(self.content, str):
            raise InvalidRequestError("Message content must be text.")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.messages, tuple)
            or not self.messages
            or not all(isinstance(message, LLMMessage) for message in self.messages)
        ):
            raise InvalidRequestError(
                "Request messages must be a non-empty tuple of LLMMessage objects."
            )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise InvalidRequestError("Response content must be text.")


@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise InvalidRequestError("Stream chunk content must be text.")


class LLMProvider(Protocol):
    """Adapters return text or raise ProviderError without logging raw content."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Return a complete response, or raise a provider error."""
        ...

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        """Yield ordered text; errors may occur while advancing the iterator."""
        ...
