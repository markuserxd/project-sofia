"""Trusted model pricing and validated usage; no SDK or credential dependency."""

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from sofia.config import ConfigurationError
from sofia.providers.base import ProviderError


def validate_model(model: str) -> None:
    if type(model) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model
    ):
        raise ConfigurationError("OpenAI model must be an explicit model identifier.")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Application-verified USD per million tokens for this exact model/tier.

    No built-in price table. Rates must cover every applicable context-length
    tier; cached tokens without a verified rate fail closed.
    """

    model: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        validate_model(self.model)
        for rate in (self.input_per_million, self.output_per_million):
            self._validate_rate(rate)
        if self.cached_input_per_million is not None:
            self._validate_rate(self.cached_input_per_million)
            if self.cached_input_per_million > self.input_per_million:
                raise ConfigurationError(
                    "Cached input rate must not exceed input rate."
                )

    @staticmethod
    def _validate_rate(rate: Decimal) -> None:
        if type(rate) is not Decimal or not rate.is_finite() or rate <= 0:
            raise ConfigurationError(
                "OpenAI pricing requires positive finite Decimal rates."
            )

    def cost(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> Decimal:
        if (
            any(
                type(value) is not int or value < 0
                for value in (input_tokens, output_tokens, cached_tokens)
            )
            or cached_tokens > input_tokens
        ):
            raise ProviderError("OpenAI pricing requires valid token counts.")
        if cached_tokens and self.cached_input_per_million is None:
            raise ProviderError("OpenAI cached input pricing is missing.")
        # In micro-USD, USD-per-million rates multiply directly by token counts.
        # Rational arithmetic avoids rounding errors from the ambient Decimal context.
        units = (
            (input_tokens - cached_tokens) * Fraction(self.input_per_million)
            + output_tokens * Fraction(self.output_per_million)
            + cached_tokens * Fraction(self.cached_input_per_million or 0)
        )
        rounded = -(-units.numerator // units.denominator)
        return Decimal((0, Decimal(rounded).as_tuple().digits, -6))


@dataclass(frozen=True, slots=True)
class OpenAIUsage:
    """Validated per-attempt metadata only; never contains user content."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int

    @classmethod
    def from_sdk(cls, usage: object) -> "OpenAIUsage":
        def count(value: object) -> int:
            if type(value) is not int or value < 0:
                raise ProviderError("OpenAI returned invalid token usage.")
            return value

        input_tokens = count(getattr(usage, "input_tokens", None))
        output_tokens = count(getattr(usage, "output_tokens", None))
        total_tokens = count(getattr(usage, "total_tokens", None))
        details = getattr(usage, "input_tokens_details", None)
        # An absent cache breakdown is charged at the full input rate.
        cached = count(getattr(details, "cached_tokens", 0))
        reasoning = count(
            getattr(
                getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0
            )
        )
        if (
            total_tokens != input_tokens + output_tokens
            or cached > input_tokens
            or reasoning > output_tokens
        ):
            raise ProviderError("OpenAI returned contradictory token usage.")
        return cls(input_tokens, output_tokens, total_tokens, cached)
