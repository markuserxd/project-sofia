"""Opt-in, synchronous Responses API adapter. Import requires the openai extra."""

import logging
import math
import os
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import openai
from openai.types.responses import ResponseInputParam

from sofia.config import ConfigurationError
from sofia.providers.base import (
    InvalidRequestError,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    ProviderAccessError,
    ProviderError,
    ProviderFundsError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    TemporaryProviderError,
)
from sofia.providers.openai_accounting import ModelPricing, OpenAIUsage, validate_model
from sofia.security.budgets import Reservation
from sofia.security.errors import AccountingBlocked, GuardrailError, security_event
from sofia.security.guardrails import RunGuardrails
from sofia.security.permissions import Action


@dataclass(frozen=True, slots=True, repr=False)
class OpenAIConfig:
    """Trusted controller configuration, never populated from model output.

    input_token_bound must locally bound ALL input tokens, including message
    framing, for this exact model. It may reject a request. No estimator fallback
    or network token-count request is made. See docs/openai-provider.md.
    """

    model: str
    pricing: ModelPricing
    input_token_bound: Callable[[LLMRequest], int]
    max_output_tokens: int
    connect_timeout: float = 5.0
    io_timeout: float = 30.0
    max_attempts: int = 1

    def __post_init__(self) -> None:
        validate_model(self.model)
        if (
            not isinstance(self.pricing, ModelPricing)
            or self.pricing.model != self.model
        ):
            raise ConfigurationError(
                "OpenAI requires pricing for the exact configured model."
            )
        if not callable(self.input_token_bound):
            raise ConfigurationError("OpenAI requires a trusted input token bound.")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 16:
            raise ConfigurationError(
                "OpenAI max_output_tokens must be an integer >= 16."
            )
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ConfigurationError("OpenAI max_attempts must be between 1 and 3.")
        for timeout in (self.connect_timeout, self.io_timeout):
            if (
                type(timeout) not in (int, float)
                or timeout > sys.float_info.max
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ConfigurationError(
                    "OpenAI timeouts must be positive finite seconds."
                )


@dataclass(slots=True, repr=False)
class _Attempt:
    reservation: Reservation
    input_bound: int
    dispatched: bool = False
    usage: OpenAIUsage | None = None
    cost: Decimal | None = None


class _BeforeDispatchError(TemporaryProviderError):
    """Only a documented connection-establishment failure may be replayed."""


def _provider_error(error: Exception) -> ProviderError:
    if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderAccessError("OpenAI authentication or access failed.")
    if isinstance(error, openai.APIStatusError) and (
        error.status_code == 402
        or error.code in ("insufficient_quota", "billing_hard_limit_reached")
    ):
        return ProviderFundsError("OpenAI API billing or quota limit reached.")
    if isinstance(error, openai.RateLimitError):
        return ProviderRateLimitError("OpenAI API rate limit reached.")
    if isinstance(error, (openai.APITimeoutError, httpx.TimeoutException)):
        return ProviderTimeoutError("OpenAI API request timed out.")
    if isinstance(
        error,
        (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError),
    ):
        return InvalidRequestError("OpenAI rejected the model or request.")
    if isinstance(
        error,
        (
            openai.APIConnectionError,
            openai.InternalServerError,
            httpx.RequestError,
        ),
    ):
        return TemporaryProviderError(
            "OpenAI network or service failure; check run accounting."
        )
    return ProviderError("OpenAI request or response failed.")


class OpenAIProvider:
    """Bind one shared run, exact model/prices, and bounded SDK options.

    The injected client/transport and bound callable are trusted application
    code. They must not perform hidden requests, retries, or log user content.
    """

    def __init__(
        self,
        *,
        config: OpenAIConfig,
        run: RunGuardrails,
        client: openai.OpenAI | None = None,
    ) -> None:
        if not isinstance(config, OpenAIConfig) or not isinstance(run, RunGuardrails):
            raise ConfigurationError(
                "OpenAI requires explicit configuration and run guardrails."
            )
        self._config = config
        self._run = run
        # The SDK's debug records include raw request bodies and exceptions.
        # Suppress third-party transport logging, including explicitly configured
        # children, without changing SOFIA's logging rules or the root logger.
        for name in ("openai", "httpx", "httpcore"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.CRITICAL + 1)
            logger.disabled = True
            for child in logging.Logger.manager.loggerDict.copy():
                if child.startswith(name + "."):
                    logging.getLogger(child).disabled = True
        timeout = httpx.Timeout(config.io_timeout, connect=config.connect_timeout)
        self._owns_client = client is None
        if client is None:
            key = os.environ.get("OPENAI_API_KEY")
            if not key or not key.strip():
                raise ConfigurationError(
                    "Set OPENAI_API_KEY to use the OpenAI provider."
                )
            if os.environ.get("OPENAI_CUSTOM_HEADERS"):
                raise ConfigurationError(
                    "OpenAI environment-defined custom headers are unsupported."
                )
            try:
                # Pin the endpoint; do not implicitly trust environment proxies or
                # redirects, which could add requests or disclose credentials.
                client = openai.OpenAI(
                    api_key=key,
                    admin_api_key="",
                    organization="",
                    project="",
                    webhook_secret="",
                    base_url="https://api.openai.com/v1",
                    max_retries=0,
                    timeout=timeout,
                    http_client=httpx.Client(
                        timeout=timeout, follow_redirects=False, trust_env=False
                    ),
                )
            except Exception:
                raise ConfigurationError(
                    "OpenAI client initialization failed."
                ) from None
        try:
            self._client = client.with_options(max_retries=0, timeout=timeout)
        except Exception:
            raise ConfigurationError("OpenAI client configuration failed.") from None

    @property
    def run_guardrails(self) -> RunGuardrails:
        """Expose run identity for trusted routing configuration validation."""
        return self._run

    @property
    def permission_scope(self) -> str:
        return f"openai:{self._config.model}"

    def close(self) -> None:
        """Close a provider-owned client; injected clients remain caller-owned."""
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                raise ProviderError("OpenAI client cleanup failed.") from None

    def _finish(self, attempt: _Attempt, failure: BaseException | None = None) -> None:
        usage = attempt.usage
        try:
            self._run.budget.settle(
                attempt.reservation,
                actual_tokens=usage.total_tokens
                if usage
                else (None if attempt.dispatched else 0),
                actual_usd=attempt.cost if attempt.dispatched else Decimal(0),
                bounds_exceeded=usage is not None
                and (
                    usage.input_tokens > attempt.input_bound
                    or usage.output_tokens > self._config.max_output_tokens
                ),
            )
        except AccountingBlocked as accounting:
            # Keep the sanitized provider/cancellation error visible AND expose
            # the latched accounting failure through its explicit cause.
            if failure is not None and not isinstance(failure, GeneratorExit):
                raise failure from accounting
            raise
        finally:
            if usage is not None:
                # Fixed messages/numeric metadata only. Broken handlers cannot
                # alter settlement or expose upstream exception payloads.
                try:
                    logging.getLogger("sofia.providers.openai").info(
                        "openai_usage input=%d output=%d cached=%d total=%d usd=%s",
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cached_tokens,
                        usage.total_tokens,
                        str(attempt.cost) if attempt.cost is not None else "unknown",
                    )
                except Exception:
                    pass

    @contextmanager
    def _attempt(self, request: LLMRequest) -> Iterator[_Attempt]:
        if not isinstance(request, LLMRequest):
            raise InvalidRequestError("Expected an LLMRequest object.")
        self._run.cancellation.checkpoint()
        try:
            bound = self._config.input_token_bound(request)
        except GuardrailError:
            raise
        except Exception:
            raise InvalidRequestError(
                "A trusted OpenAI input token bound is unavailable."
            ) from None
        if type(bound) is not int or bound <= 0:
            raise InvalidRequestError(
                "OpenAI input token bound must be a positive integer."
            )
        cost = self._config.pricing.cost(bound, self._config.max_output_tokens)
        attempt = _Attempt(
            self._run.begin_request(
                self.permission_scope,
                estimated_tokens=bound + self._config.max_output_tokens,
                estimated_usd=cost,
            ),
            bound,
        )
        try:
            yield attempt
        except BaseException as failure:
            self._finish(attempt, failure)
            raise
        else:
            self._finish(attempt)

    @contextmanager
    def _exchange(
        self, request: LLMRequest, *, stream: bool
    ) -> Iterator[tuple[Any, _Attempt]]:
        for index in range(self._config.max_attempts):
            try:
                with self._attempt(request) as attempt:
                    try:
                        messages: ResponseInputParam = [
                            {"role": message.role, "content": message.content}
                            for message in request.messages
                        ]
                        self._run.permissions.check(
                            Action.API_REQUEST, self.permission_scope
                        )
                        self._run.cancellation.checkpoint()
                        attempt.dispatched = True
                        try:
                            result = self._client.responses.create(
                                model=self._config.model,
                                input=messages,
                                max_output_tokens=self._config.max_output_tokens,
                                stream=stream,
                                store=False,
                                background=False,
                                tools=[],
                                service_tier="default",
                                truncation="disabled",
                            )
                        except openai.APIConnectionError as error:
                            if isinstance(
                                error.__cause__,
                                (
                                    httpx.ConnectError,
                                    httpx.ConnectTimeout,
                                    httpx.PoolTimeout,
                                ),
                            ):
                                attempt.dispatched = False
                                raise _BeforeDispatchError(
                                    "OpenAI connection establishment failed."
                                ) from None
                            raise
                        yield result, attempt
                        return
                    except (GuardrailError, ProviderError):
                        raise
                    except Exception as error:
                        self._run.cancellation.checkpoint()
                        raise _provider_error(error) from None
            except _BeforeDispatchError:
                self._run.cancellation.checkpoint()
                if index + 1 == self._config.max_attempts:
                    raise TemporaryProviderError(
                        "OpenAI connection attempts exhausted."
                    ) from None

    def _record_usage(self, response: Any, attempt: _Attempt) -> None:
        if response.model != self._config.model or response.service_tier not in (
            None,
            "default",
        ):
            raise ProviderError(
                "OpenAI response model or pricing tier did not match configuration."
            )
        attempt.usage = OpenAIUsage.from_sdk(response.usage)
        # Cache writes have a distinct price on some models; this adapter has
        # no verified rate for them and must not count them as ordinary input.
        details = getattr(response.usage, "input_tokens_details", None)
        writes = getattr(details, "cache_write_tokens", None)
        if writes is not None and (type(writes) is not int or writes != 0):
            raise ProviderError("OpenAI returned unsupported billable usage.")
        attempt.cost = self._config.pricing.cost(
            attempt.usage.input_tokens,
            attempt.usage.output_tokens,
            attempt.usage.cached_tokens,
        )

    @staticmethod
    def _text(response: Any) -> str:
        if (
            response.status != "completed"
            or response.error is not None
            or response.incomplete_details is not None
        ):
            raise ProviderError("OpenAI response did not complete successfully.")
        pieces: list[str] = []
        if not isinstance(response.output, list):
            raise ProviderError("OpenAI returned malformed text output.")
        for item in response.output:
            if item.type == "reasoning":
                continue
            if (
                item.type != "message"
                or item.role != "assistant"
                or item.status != "completed"
            ):
                raise ProviderError("OpenAI returned unsupported output.")
            for part in item.content:
                if part.type != "output_text" or type(part.text) is not str:
                    raise ProviderError("OpenAI returned unsupported text content.")
                pieces.append(part.text)
        return "".join(pieces)

    def generate(self, request: LLMRequest) -> LLMResponse:
        with self._exchange(request, stream=False) as (response, attempt):
            self._record_usage(response, attempt)
            self._run.cancellation.checkpoint()
            text = self._text(response)
        self._run.cancellation.checkpoint()
        return LLMResponse(text)

    def stream(self, request: LLMRequest) -> Generator[LLMStreamChunk, None, None]:
        with self._exchange(request, stream=True) as (events, attempt):
            pieces: list[str] = []
            try:
                iterator = iter(events)
                while True:
                    self._run.cancellation.checkpoint()
                    try:
                        event = next(iterator)
                    except StopIteration:
                        raise ProviderError(
                            "OpenAI stream ended without completion."
                        ) from None
                    # Record terminal usage before observing a stop raised while
                    # blocked in next(), so completed usage is not discarded.
                    if event.type in (
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    ):
                        self._record_usage(event.response, attempt)
                        self._run.cancellation.checkpoint()
                        text = self._text(event.response)
                        if event.type != "response.completed" or text != "".join(
                            pieces
                        ):
                            raise ProviderError(
                                "OpenAI stream completion was inconsistent."
                            )
                        break
                    self._run.cancellation.checkpoint()
                    if event.type == "response.output_text.delta":
                        if type(event.delta) is not str:
                            raise ProviderError(
                                "OpenAI returned an invalid text delta."
                            )
                        pieces.append(event.delta)
                        yield LLMStreamChunk(event.delta)
                    elif event.type in (
                        "response.output_item.added",
                        "response.output_item.done",
                    ):
                        if event.item.type not in ("message", "reasoning"):
                            raise ProviderError(
                                "OpenAI stream returned unsupported output."
                            )
                    elif event.type in (
                        "response.content_part.added",
                        "response.content_part.done",
                    ):
                        if event.part.type != "output_text":
                            raise ProviderError(
                                "OpenAI stream returned unsupported content."
                            )
                    elif event.type not in (
                        "response.created",
                        "response.in_progress",
                        "response.output_text.done",
                    ):
                        raise ProviderError(
                            "OpenAI stream failed or returned an unsupported event."
                        )
            finally:
                failed = sys.exc_info()[0] is not None
                try:
                    events.close()
                except Exception:
                    security_event("openai_stream_cleanup_failed")
                    if not failed:
                        raise ProviderError("OpenAI stream cleanup failed.") from None
        self._run.cancellation.checkpoint()
