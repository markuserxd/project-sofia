"""Explicit controller-owned routes; no discovery, model parsing, or approval."""

import re
import sys
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sofia.config import ConfigurationError
from sofia.providers.base import (
    InvalidRequestError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    UndispatchedProviderError,
)
from sofia.providers.mock import MockLLMProvider
from sofia.security.errors import AccountingBlocked
from sofia.security.guardrails import RunGuardrails


@runtime_checkable
class GuardedProvider(LLMProvider, Protocol):
    """Paid adapters expose their actual read-only run identity."""

    @property
    def run_guardrails(self) -> RunGuardrails: ...


class ModelRouter:
    def __init__(
        self,
        *,
        run: RunGuardrails,
        routes: Mapping[str, LLMProvider] | None = None,
        fallbacks: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(run, RunGuardrails):
            raise ConfigurationError("Routing requires one shared run context.")
        if (routes is not None and not isinstance(routes, Mapping)) or (
            fallbacks is not None and not isinstance(fallbacks, Mapping)
        ):
            raise ConfigurationError("Routes and fallbacks must be explicit mappings.")
        self._run = run
        self._routes: dict[str, LLMProvider] = {"mock": MockLLMProvider()}
        if routes is not None:
            self._routes.update(routes)
        self._fallbacks = dict(fallbacks or {})
        for name, provider in self._routes.items():
            if type(name) is not str or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name
            ):
                raise ConfigurationError(
                    "Route identifiers must be safe explicit names."
                )
            if type(provider) is MockLLMProvider:
                continue
            if (
                not isinstance(provider, GuardedProvider)
                or provider.run_guardrails is not run
            ):
                raise ConfigurationError(
                    "All paid routes must expose the same RunGuardrails instance."
                )
        if type(self._routes["mock"]) is not MockLLMProvider:
            raise ConfigurationError("The default mock route must stay offline.")
        for source, destination in self._fallbacks.items():
            if (
                type(source) is not str
                or type(destination) is not str
                or source not in self._routes
                or destination not in self._routes
            ):
                raise ConfigurationError("Fallback routes must already be configured.")
            seen: set[str] = set()
            current = source
            while current in self._fallbacks:
                if current in seen:
                    raise ConfigurationError("Fallback routes must not form a cycle.")
                seen.add(current)
                current = self._fallbacks[current]

    def select(self, route: str = "mock") -> "RoutedProvider":
        """Trusted application decision; never read route names from prompts."""
        if type(route) is not str or route not in self._routes:
            raise InvalidRequestError("Unknown model route.")
        return RoutedProvider(self, route)

    def _checkpoint(self) -> None:
        self._run.cancellation.checkpoint()
        if self._run.budget.snapshot.blocked:
            raise AccountingBlocked()


@dataclass(frozen=True, slots=True, repr=False)
class RoutedProvider:
    router: ModelRouter
    route: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        route = self.route
        while True:
            self.router._checkpoint()
            try:
                response = self.router._routes[route].generate(request)
            except UndispatchedProviderError:
                self.router._checkpoint()
                if route not in self.router._fallbacks:
                    raise
                route = self.router._fallbacks[route]
            else:
                self.router._checkpoint()
                return response

    def stream(self, request: LLMRequest) -> Generator[LLMStreamChunk, None, None]:
        route = self.route
        while True:
            self.router._checkpoint()
            emitted = False
            iterator = None
            try:
                iterator = iter(self.router._routes[route].stream(request))
                while True:
                    self.router._checkpoint()
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        self.router._checkpoint()
                        return
                    self.router._checkpoint()
                    emitted = True  # Even an empty chunk is user-visible output.
                    yield chunk
            except UndispatchedProviderError:
                self.router._checkpoint()
                if emitted or route not in self.router._fallbacks:
                    raise
                route = self.router._fallbacks[route]
            finally:
                if iterator is not None:
                    close = getattr(iterator, "close", None)
                    if close is not None:
                        failure = sys.exc_info()[1]
                        try:
                            close()
                        except AccountingBlocked as accounting:
                            if failure is not None and not isinstance(
                                failure, GeneratorExit
                            ):
                                raise failure from accounting
                            raise
