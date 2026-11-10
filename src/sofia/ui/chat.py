"""Trusted desktop chat configuration, request composition and provider lifetime."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import TYPE_CHECKING

from sofia.persona import PersonaConfig, compose_request
from sofia.providers import LLMMessage, LLMRequest
from sofia.providers.anthropic_accounting import AnthropicPricing
from sofia.providers.base import ProviderError
from sofia.providers.openai_accounting import ModelPricing
from sofia.security import Action, PermissionDenied, RunGuardrails
from sofia.security.errors import security_event

if TYPE_CHECKING:
    from sofia.providers.anthropic_provider import (
        AnthropicProvider as _AnthropicProvider,
    )
    from sofia.providers.openai_provider import OpenAIProvider as _OpenAIProvider

PROVIDERS = ("Offline Mock", "OpenAI", "Anthropic Claude")
OPENAI_MODEL = "gpt-4.1-mini-2025-04-14"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_PRICING = ModelPricing(
    OPENAI_MODEL, Decimal("0.40"), Decimal("1.60"), Decimal("0.10")
)
MIN_OUTPUT = 16
MAX_OUTPUT = 2048
MAX_REPLY = 16_384
MAX_TURNS = 12
MAX_CONTEXT_BYTES = 64_000


def models_for(provider: str) -> tuple[str, ...]:
    if provider == "OpenAI":
        return (OPENAI_MODEL,)
    if provider == "Anthropic Claude":
        return (ANTHROPIC_MODEL,)
    return ("",)


@dataclass(frozen=True, slots=True)
class ChatSettings:
    provider: str = "Offline Mock"
    model: str = ""
    paid_enabled: bool = False
    max_output_tokens: int = 512
    budget_cents: int = 50

    def __post_init__(self) -> None:
        if (
            type(self.provider) is not str
            or self.provider not in PROVIDERS
            or type(self.model) is not str
            or self.model not in models_for(self.provider)
            or type(self.paid_enabled) is not bool
            or (self.paid_enabled and self.offline)
            or type(self.max_output_tokens) is not int
            or not MIN_OUTPUT <= self.max_output_tokens <= MAX_OUTPUT
            or type(self.budget_cents) is not int
            or not 1 <= self.budget_cents <= 100
        ):
            raise ValueError("invalid_chat_settings")

    @property
    def offline(self) -> bool:
        return self.provider == "Offline Mock"

    @property
    def route(self) -> tuple[str, str]:
        return self.provider, self.model

    @property
    def name(self) -> str:
        return self.provider if self.offline else f"{self.provider} · {self.model}"

    @property
    def credential(self) -> str:
        return "OPENAI_API_KEY" if self.provider == "OpenAI" else "ANTHROPIC_API_KEY"

    @property
    def credential_configured(self) -> bool:
        return not self.offline and bool(os.environ.get(self.credential, "").strip())

    @property
    def scope(self) -> str:
        provider = "openai" if self.provider == "OpenAI" else "anthropic"
        return f"{provider}:{self.model}"

    @property
    def max_usd(self) -> Decimal:
        return Decimal(f"{self.budget_cents // 100}.{self.budget_cents % 100:02}")

    @property
    def context_window(self) -> int:
        # Use the published model window, including hidden message framing.
        # The local UTF-8 limit bounds retained content, not billable tokens.
        if self.model == OPENAI_MODEL:
            return 1_047_576
        return AnthropicPricing(self.model).context_window

    def input_bound(self, _request: LLMRequest) -> int:
        return self.context_window

    @property
    def reservation_usd(self) -> Decimal:
        if self.model == OPENAI_MODEL:
            return _OPENAI_PRICING.cost(self.context_window, self.max_output_tokens)
        return AnthropicPricing(self.model).reservation(
            self.context_window, self.max_output_tokens
        )

    def new_run(self) -> RunGuardrails:
        return RunGuardrails(
            max_steps=1,
            max_tokens=self.context_window + self.max_output_tokens,
            max_usd=self.max_usd,
        )


def compose_chat_request(
    message: str, persona: PersonaConfig, history: tuple[LLMMessage, ...]
) -> LLMRequest:
    system, user = compose_request(message, persona, mode="text_only").messages
    request = LLMRequest((system, *history, user))
    if len(history) >= 2 * MAX_TURNS or (
        sum(len(item.content.encode("utf-8")) for item in request.messages)
        > MAX_CONTEXT_BYTES
    ):
        raise ValueError("context_limit")
    return request


class ChatStream:
    """One bounded buffer per worker. The GUI polls it without queuing every delta."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._text = ""

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    def append(self, text: str) -> None:
        with self._lock:
            if len(self._text) + len(text) > MAX_REPLY:
                raise OutputLimitError()
            # Provider chunks are decoded strings, possibly split inside graphemes.
            text.encode("utf-8")
            self._text += text


@dataclass(frozen=True, slots=True, repr=False)
class ChatWork:
    settings: ChatSettings
    request: LLMRequest
    stream: ChatStream = field(default_factory=ChatStream)


class OutputLimitError(ProviderError):
    """The local display limit was reached before successful completion."""


@contextmanager
def _close_after(close: Callable[[], None]) -> Iterator[None]:
    try:
        yield
    except BaseException:
        try:
            close()
        except Exception:
            security_event("chat_cleanup_failed")
        raise
    else:
        close()


def stream_chat(
    settings: ChatSettings, request: LLMRequest, run: RunGuardrails, stream: ChatStream
) -> None:
    if not settings.paid_enabled:
        raise PermissionDenied()
    run.permissions.check(Action.API_REQUEST, settings.scope)
    provider: _OpenAIProvider | _AnthropicProvider
    # Import only the selected SDK, and only on an explicitly authorized worker.
    if settings.provider == "OpenAI":
        from sofia.providers.openai_provider import OpenAIConfig, OpenAIProvider

        provider = OpenAIProvider(
            config=OpenAIConfig(
                settings.model,
                _OPENAI_PRICING,
                settings.input_bound,
                settings.max_output_tokens,
                io_timeout=15.0,
            ),
            run=run,
        )
    else:
        from sofia.providers.anthropic_provider import (
            AnthropicConfig,
            AnthropicProvider,
        )

        provider = AnthropicProvider(
            config=AnthropicConfig(
                settings.model,
                settings.input_bound,
                settings.max_output_tokens,
                io_timeout=15.0,
            ),
            run=run,
        )
    with _close_after(provider.close):
        chunks = provider.stream(request)
        with _close_after(chunks.close):
            for chunk in chunks:
                run.cancellation.checkpoint()
                stream.append(chunk.content)
    run.cancellation.checkpoint()
