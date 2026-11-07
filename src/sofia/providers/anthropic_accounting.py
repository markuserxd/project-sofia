"""Verified first-party standard/global Claude pricing and strict usage parsing."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sofia.config import ConfigurationError
from sofia.providers.base import ProviderError


def fields(value: Any) -> dict[str, Any]:
    """Keep unknown SDK fields so new billing categories cannot disappear."""
    if isinstance(value, dict):
        return value.copy()
    if callable(getattr(value, "to_dict", None)):
        # SDK/Pydantic serialization warnings can include malformed raw values.
        # Preserve those values for our strict validation without printing them.
        result = value.to_dict(warnings=False)
        if isinstance(result, dict):
            return result
    raise ProviderError("Anthropic returned malformed metadata.")


def count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderError("Anthropic returned invalid token usage.")
    return value


@dataclass(frozen=True, slots=True)
class AnthropicUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens

    @classmethod
    def from_sdk(cls, value: object) -> "AnthropicUsage":
        data = fields(value)
        allowed = {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cache_creation",
            "service_tier",
            "inference_geo",
            "speed",
            "server_tool_use",
            "output_tokens_details",
        }
        if data.keys() - allowed:
            raise ProviderError("Anthropic returned unknown billable usage.")
        if (
            data.get("service_tier") not in (None, "standard")
            or data.get("inference_geo") not in (None, "global")
            or data.get("speed") not in (None, "standard")
        ):
            raise ProviderError("Anthropic returned unsupported billing mode.")
        inputs = count(data.get("input_tokens"))
        outputs = count(data.get("output_tokens"))
        # These optional SDK categories are absent/null when inapplicable.
        writes = count(
            data.get("cache_creation_input_tokens")
            if data.get("cache_creation_input_tokens") is not None
            else 0
        )
        reads = count(
            data.get("cache_read_input_tokens")
            if data.get("cache_read_input_tokens") is not None
            else 0
        )
        short = long = 0
        breakdown = data.get("cache_creation")
        if breakdown is not None:
            cache = fields(breakdown)
            if cache.keys() != {
                "ephemeral_5m_input_tokens",
                "ephemeral_1h_input_tokens",
            }:
                raise ProviderError("Anthropic returned unsupported cache usage.")
            short = count(cache["ephemeral_5m_input_tokens"])
            long = count(cache["ephemeral_1h_input_tokens"])
        if short + long != writes:
            raise ProviderError(
                "Anthropic cache duration breakdown is missing or inconsistent."
            )
        tools = data.get("server_tool_use")
        if tools is not None:
            tool_counts = fields(tools)
            if tool_counts.keys() - {
                "web_search_requests",
                "web_fetch_requests",
            } or any(count(v) != 0 for v in tool_counts.values()):
                raise ProviderError("Anthropic returned unsupported tool billing.")
        details = data.get("output_tokens_details")
        if details is not None:
            output_details = fields(details)
            if (
                output_details.keys() != {"thinking_tokens"}
                or count(output_details["thinking_tokens"]) != 0
            ):
                raise ProviderError("Anthropic returned unsupported thinking usage.")
        return cls(inputs, outputs, reads, short, long)


@dataclass(frozen=True, slots=True)
class AnthropicPricing:
    """Small allowlist; verify rates/limits before adding another exact model.

    Rates checked 2026-11-07 against the official model and pricing pages.
    Only first-party, standard speed/tier, global, synchronous Messages billing.
    """

    model: str

    def __post_init__(self) -> None:
        if type(self.model) is not str or self.model not in (
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
        ):
            raise ConfigurationError(
                "Anthropic model has no verified pricing and limits."
            )

    @property
    def context_window(self) -> int:
        return 200_000 if self.model == "claude-haiku-4-5-20251001" else 1_000_000

    @property
    def max_output_tokens(self) -> int:
        return 64_000 if self.model == "claude-haiku-4-5-20251001" else 128_000

    @property
    def rates(self) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        if self.model == "claude-haiku-4-5-20251001":
            return (
                Decimal("1"),
                Decimal("5"),
                Decimal("0.10"),
                Decimal("1.25"),
                Decimal("2"),
            )
        return (
            Decimal("3"),
            Decimal("15"),
            Decimal("0.30"),
            Decimal("3.75"),
            Decimal("6"),
        )

    def cost(self, usage: AnthropicUsage) -> Decimal:
        counts = (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_5m_tokens,
            usage.cache_write_1h_tokens,
        )
        units = sum(
            (count(n) * Fraction(rate) for n, rate in zip(counts, self.rates)),
            Fraction(0),
        )
        # USD-per-million * tokens is micro-USD. Round once upward, independent
        # of ambient Decimal precision, as required by the shared ledger.
        rounded = -(-units.numerator // units.denominator)
        return Decimal((0, Decimal(rounded).as_tuple().digits, -6))

    def reservation(self, inputs: int, outputs: int) -> Decimal:
        # All input could be charged at the most expensive supported cache rate.
        return self.cost(AnthropicUsage(0, outputs, 0, 0, inputs))
