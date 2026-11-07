"""Opt-in synchronous text-only Claude adapter (optional anthropic extra)."""

import logging
import math
import os
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic
import httpx2

from sofia.config import ConfigurationError
from sofia.providers.anthropic_accounting import (
    AnthropicPricing,
    AnthropicUsage,
    fields,
)
from sofia.providers.anthropic_streaming import TextStream
from sofia.providers.base import (
    InvalidRequestError,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderError,
    TemporaryProviderError,
    UndispatchedProviderError,
)
from sofia.security.budgets import Reservation
from sofia.security.errors import AccountingBlocked, GuardrailError, security_event
from sofia.security.guardrails import RunGuardrails
from sofia.security.permissions import Action

_PRIVATE_SDK_CALL: ContextVar[bool] = ContextVar(
    "anthropic_private_call", default=False
)


class _PrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_SDK_CALL.get()


@contextmanager
def _private_sdk_call() -> Iterator[None]:
    # Source-logger filters also cover explicitly attached child handlers.
    # Context-local suppression does not disable other clients/threads or keep
    # logging disabled while a consumer is holding a suspended stream iterator.
    names = {
        "anthropic",
        "httpx2",
        "httpcore2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
    }
    names.update(
        name
        for name in logging.Logger.manager.loggerDict.copy()
        if name.startswith(("anthropic.", "httpx2.", "httpcore2."))
    )
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _PrivacyFilter) for f in logger.filters):
            logger.addFilter(_PrivacyFilter())
    token = _PRIVATE_SDK_CALL.set(True)
    try:
        yield
    finally:
        _PRIVATE_SDK_CALL.reset(token)


@dataclass(frozen=True, slots=True, repr=False)
class AnthropicConfig:
    model: str
    input_token_bound: Callable[[LLMRequest], int]
    max_output_tokens: int
    connect_timeout: float = 5.0
    io_timeout: float = 30.0

    def __post_init__(self) -> None:
        pricing = AnthropicPricing(self.model)
        if not callable(self.input_token_bound):
            raise ConfigurationError(
                "Anthropic requires a trusted model-specific input token bound."
            )
        if (
            type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= pricing.max_output_tokens
        ):
            raise ConfigurationError(
                "Anthropic output cap exceeds verified model limits."
            )
        for timeout in (self.connect_timeout, self.io_timeout):
            if (
                type(timeout) not in (int, float)
                or timeout > sys.float_info.max
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ConfigurationError(
                    "Anthropic timeouts must be positive finite seconds."
                )


@dataclass(slots=True, repr=False)
class _Attempt:
    reservation: Reservation
    input_bound: int
    dispatched: bool = False
    usage: AnthropicUsage | None = None
    cost: Decimal | None = None
    bounds_exceeded: bool = False


def _provider_error(error: Exception) -> ProviderError:
    if isinstance(
        error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
    ):
        return ProviderError("anthropic_access_failed")
    if isinstance(
        error,
        (
            anthropic.BadRequestError,
            anthropic.NotFoundError,
            anthropic.UnprocessableEntityError,
        ),
    ):
        return InvalidRequestError("anthropic_invalid_request")
    if isinstance(error, anthropic.APITimeoutError):
        return TemporaryProviderError("anthropic_timeout")
    if isinstance(error, anthropic.RateLimitError):
        return TemporaryProviderError("anthropic_rate_limited")
    if isinstance(error, (anthropic.APIConnectionError, httpx2.RequestError)):
        return TemporaryProviderError("anthropic_connection_failed")
    if isinstance(error, anthropic.InternalServerError):
        return TemporaryProviderError("anthropic_server_failed")
    return ProviderError("anthropic_request_or_response_failed")


def _messages(request: LLMRequest) -> dict[str, Any]:
    if not isinstance(request, LLMRequest):
        raise InvalidRequestError("Expected an LLMRequest object.")
    system: list[dict[str, str]] = []
    messages: list[dict[str, str]] = []
    for message in request.messages:
        if message.role == "system":
            if messages:
                raise InvalidRequestError(
                    "Anthropic requires system instructions before conversation turns."
                )
            system.append({"type": "text", "text": message.content})
        else:
            # The API merges adjacent same-role turns. Reject rather than silently
            # changing the source conversation or relying on model-specific prefill.
            expected = (
                "assistant" if messages and messages[-1]["role"] == "user" else "user"
            )
            if message.role != expected:
                raise InvalidRequestError(
                    "Anthropic requires alternating user and assistant turns."
                )
            messages.append({"role": message.role, "content": message.content})
    if not messages or messages[-1]["role"] != "user":
        raise InvalidRequestError(
            "Anthropic requires a final user turn; assistant prefill is unsupported."
        )
    result: dict[str, Any] = {"messages": messages}
    if system:
        result["system"] = system
    return result


class AnthropicProvider:
    """Injected clients/transports and token bounds are trusted controller code."""

    def __init__(
        self,
        *,
        config: AnthropicConfig,
        run: RunGuardrails,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        if not isinstance(config, AnthropicConfig) or not isinstance(
            run, RunGuardrails
        ):
            raise ConfigurationError(
                "Anthropic requires explicit configuration and shared run guardrails."
            )
        self._config = config
        self._run = run
        self._pricing = AnthropicPricing(config.model)
        self._owns_client = client is None
        timeout = httpx2.Timeout(config.io_timeout, connect=config.connect_timeout)
        try:
            with _private_sdk_call():
                if client is None:
                    key = os.environ.get("ANTHROPIC_API_KEY")
                    if not key or not key.strip():
                        raise ConfigurationError(
                            "Set ANTHROPIC_API_KEY to use the Anthropic provider."
                        )
                    if os.environ.get("ANTHROPIC_CUSTOM_HEADERS"):
                        raise ConfigurationError(
                            "Anthropic environment-defined custom headers are unsupported."
                        )
                    client = anthropic.Anthropic(
                        api_key=key,
                        webhook_key="",
                        base_url="https://api.anthropic.com",
                        max_retries=0,
                        timeout=timeout,
                        open_telemetry=False,
                        http_client=httpx2.Client(
                            timeout=timeout, follow_redirects=False, trust_env=False
                        ),
                    )
                self._client = client.with_options(
                    max_retries=0, timeout=timeout, open_telemetry=False
                )
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError(
                "Anthropic client initialization failed."
            ) from None

    @property
    def run_guardrails(self) -> RunGuardrails:
        return self._run

    @property
    def permission_scope(self) -> str:
        return f"anthropic:{self._config.model}"

    def close(self) -> None:
        if self._owns_client:
            try:
                with _private_sdk_call():
                    self._client.close()
            except Exception:
                raise ProviderError("anthropic_client_cleanup_failed") from None

    def _finish(self, attempt: _Attempt, failure: BaseException | None = None) -> None:
        usage = attempt.usage
        try:
            self._run.budget.settle(
                attempt.reservation,
                actual_tokens=usage.total_tokens
                if usage
                else (None if attempt.dispatched else 0),
                actual_usd=attempt.cost if attempt.dispatched else Decimal(0),
                bounds_exceeded=attempt.bounds_exceeded,
            )
        except AccountingBlocked as accounting:
            if failure is not None and not isinstance(failure, GeneratorExit):
                raise failure from accounting
            raise
        finally:
            if usage is not None:
                try:
                    logging.getLogger("sofia.providers.anthropic").info(
                        "anthropic_usage input=%d output=%d cache_read=%d cache_5m=%d cache_1h=%d total=%d usd=%s",
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_read_tokens,
                        usage.cache_write_5m_tokens,
                        usage.cache_write_1h_tokens,
                        usage.total_tokens,
                        str(attempt.cost),
                    )
                except Exception:
                    pass

    @contextmanager
    def _exchange(
        self, request: LLMRequest, *, stream: bool
    ) -> Iterator[tuple[Any, _Attempt]]:
        arguments = _messages(request)
        self._run.cancellation.checkpoint()
        try:
            bound = self._config.input_token_bound(request)
        except GuardrailError:
            raise
        except Exception:
            raise InvalidRequestError(
                "A trusted Anthropic input token bound is unavailable."
            ) from None
        if type(bound) is not int or not 0 < bound <= self._pricing.context_window:
            raise InvalidRequestError(
                "Anthropic input token bound is outside verified model limits."
            )
        attempt = _Attempt(
            self._run.begin_request(
                self.permission_scope,
                estimated_tokens=bound + self._config.max_output_tokens,
                estimated_usd=self._pricing.reservation(
                    bound, self._config.max_output_tokens
                ),
            ),
            bound,
        )
        try:
            try:
                self._run.permissions.check(Action.API_REQUEST, self.permission_scope)
                self._run.cancellation.checkpoint()
                attempt.dispatched = True
                try:
                    with _private_sdk_call():
                        result = self._client.messages.create(
                            model=self._config.model,
                            max_tokens=self._config.max_output_tokens,
                            stream=stream,
                            service_tier="standard_only",
                            thinking={"type": "disabled"},
                            **arguments,
                        )
                except anthropic.APIConnectionError as error:
                    if isinstance(
                        error.__cause__,
                        (
                            httpx2.ConnectError,
                            httpx2.ConnectTimeout,
                            httpx2.PoolTimeout,
                        ),
                    ):
                        attempt.dispatched = False
                        raise UndispatchedProviderError() from None
                    raise
                yield result, attempt
            except (GuardrailError, ProviderError):
                raise
            except Exception as error:
                self._run.cancellation.checkpoint()
                raise _provider_error(error) from None
        except BaseException as failure:
            self._finish(attempt, failure)
            raise
        else:
            self._finish(attempt)

    def _record_usage(self, response: dict[str, Any], attempt: _Attempt) -> None:
        if response.get("container") is not None:
            raise ProviderError("anthropic_container_billing_unsupported")
        if response.get("model") != self._config.model:
            raise ProviderError("anthropic_response_model_mismatch")
        usage = AnthropicUsage.from_sdk(response.get("usage"))
        cost = self._pricing.cost(usage)
        attempt.usage, attempt.cost = usage, cost
        attempt.bounds_exceeded = (
            usage.total_input_tokens > attempt.input_bound
            or usage.output_tokens > self._config.max_output_tokens
        )

    @staticmethod
    def _text(response: dict[str, Any]) -> str:
        if (
            response.get("type") != "message"
            or response.get("role") != "assistant"
            or response.get("stop_reason") != "end_turn"
            or response.get("stop_sequence") is not None
            or response.get("stop_details") is not None
        ):
            raise ProviderError("anthropic_response_incomplete")
        blocks = response.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("anthropic_content_malformed")
        pieces = []
        for block in blocks:
            if (
                block.get("type") != "text"
                or type(block.get("text")) is not str
                or block.get("citations")
            ):
                raise ProviderError("anthropic_content_unsupported")
            pieces.append(block["text"])
        return "".join(pieces)

    def generate(self, request: LLMRequest) -> LLMResponse:
        with self._exchange(request, stream=False) as (result, attempt):
            response = fields(result)
            self._record_usage(response, attempt)
            self._run.cancellation.checkpoint()
            text = self._text(response)
        self._run.cancellation.checkpoint()
        return LLMResponse(text)

    def stream(self, request: LLMRequest) -> Generator[LLMStreamChunk, None, None]:
        with self._exchange(request, stream=True) as (events, attempt):
            state = TextStream()
            try:
                iterator = iter(events)
                while not state.stopped:
                    self._run.cancellation.checkpoint()
                    try:
                        with _private_sdk_call():
                            event = next(iterator)
                    except StopIteration:
                        raise ProviderError(
                            "anthropic_stream_missing_completion"
                        ) from None
                    if (
                        event.type == "message_start"
                        and event.message.model != self._config.model
                    ):
                        raise ProviderError("anthropic_response_model_mismatch")
                    text = state.accept(event)
                    if state.stopped:
                        assert state.message is not None
                        self._record_usage(state.message, attempt)
                        self._text(state.message)
                    self._run.cancellation.checkpoint()
                    if text is not None:
                        yield LLMStreamChunk(text)
            finally:
                failed = sys.exc_info()[0] is not None
                try:
                    with _private_sdk_call():
                        events.close()
                except Exception:
                    security_event("anthropic_stream_cleanup_failed")
                    if not failed:
                        raise ProviderError("anthropic_stream_cleanup_failed") from None
        self._run.cancellation.checkpoint()
