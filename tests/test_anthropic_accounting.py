from decimal import Decimal, localcontext

import anthropic
import pytest
from anthropic_helpers import MODEL, usage

from sofia.config import ConfigurationError
from sofia.providers import ProviderError
from sofia.providers.anthropic_accounting import AnthropicPricing, AnthropicUsage


def test_cache_categories_are_disjoint_and_cost_is_exact():
    parsed = AnthropicUsage.from_sdk(anthropic.types.Usage.model_construct(**usage()))
    assert parsed == AnthropicUsage(10, 5, 7, 3, 2)
    assert parsed.total_input_tokens == 22
    assert parsed.total_tokens == 27
    prices = AnthropicPricing(MODEL)
    # 10*1 + 5*5 + 7*.1 + 3*1.25 + 2*2 = 43.45 micro-USD.
    with localcontext() as context:
        context.prec = 1
        assert prices.cost(parsed) == Decimal("0.000044")
        assert prices.cost(AnthropicUsage(0, 0, 1, 0, 0)) == Decimal("0.000001")
        assert prices.reservation(100, 32) == Decimal("0.000360")
        assert prices.cost(AnthropicUsage(10**30, 0, 0, 0, 0)) == Decimal("1e24")


def test_model_specific_prices():
    prices = AnthropicPricing("claude-sonnet-4-6")
    assert prices.cost(
        AnthropicUsage(1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    ) == Decimal("28.050000")
    assert prices.context_window == 1_000_000
    with pytest.raises(ConfigurationError):
        AnthropicPricing("claude-latest")


@pytest.mark.parametrize(
    "change",
    [
        {"input_tokens": None},
        {"input_tokens": -1},
        {"input_tokens": True},
        {"output_tokens": 1.5},
        {"output_tokens": "5"},
        {"cache_read_input_tokens": -1},
        {"cache_creation_input_tokens": 6},
        {"cache_creation": None},
        {"cache_creation": {"ephemeral_5m_input_tokens": 5}},
        {
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3,
                "ephemeral_1h_input_tokens": 2,
                "new_duration": 0,
            }
        },
        {"service_tier": "priority"},
        {"service_tier": "batch"},
        {"speed": "fast"},
        {"inference_geo": "us"},
        {"new_billable_tokens": 0},
        {"server_tool_use": {"web_search_requests": 1}},
        {"server_tool_use": {"unknown_requests": 0}},
        {"output_tokens_details": {"thinking_tokens": 1}},
        {"output_tokens_details": {"new_tokens": 0}},
    ],
)
def test_malformed_or_unsupported_usage_fails_closed(change):
    with pytest.raises(ProviderError):
        AnthropicUsage.from_sdk(usage(**change))


def test_absent_inapplicable_optional_categories_and_valid_zero_breakdowns():
    assert (
        AnthropicUsage.from_sdk({"input_tokens": 3, "output_tokens": 0}).total_tokens
        == 3
    )
    assert (
        AnthropicUsage.from_sdk(
            usage(
                server_tool_use={"web_search_requests": 0, "web_fetch_requests": 0},
                output_tokens_details={"thinking_tokens": 0},
            )
        ).total_tokens
        == 27
    )
