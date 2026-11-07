from decimal import Decimal

import httpx2
import pytest
from anthropic_helpers import REQUEST, EventBody, events

from sofia.providers import ProviderError
from sofia.security import AccountingBlocked, OperationCancelled


def streamed(claude, items=None, **body_options):
    body = EventBody(events() if items is None else items, **body_options)
    provider, run, calls = claude(lambda req, run: httpx2.Response(200, stream=body))
    return provider, run, calls, body


def test_ordered_unicode_empty_deltas_and_final_usage(claude):
    provider, run, calls, body = streamed(claude)
    assert [c.content for c in provider.stream(REQUEST)] == [
        "private-answer ",
        "",
        "ไทย🙂",
    ]
    assert run.budget.snapshot.committed_tokens == 27
    assert run.budget.snapshot.committed_usd == Decimal("0.000044")
    assert body.closed
    assert not run.budget.snapshot.blocked


def test_empty_completed_response(claude):
    provider, run, calls, body = streamed(claude, events(texts=()))
    assert list(provider.stream(REQUEST)) == []
    assert body.closed
    assert run.budget.snapshot.committed_tokens == 27


def test_cumulative_delta_usage_overwrites_without_double_counting(claude):
    items = events(texts=("a",))
    items.insert(
        -1,
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {
                "output_tokens": 7,
                "input_tokens": 12,
                "cache_read_input_tokens": 8,
            },
        },
    )
    provider, run, calls, body = streamed(claude, items)
    assert "".join(c.content for c in provider.stream(REQUEST)) == "a"
    assert run.budget.snapshot.committed_tokens == 32  # 12+8+5+7
    assert run.budget.snapshot.committed_usd == Decimal("0.000056")


@pytest.mark.parametrize("partial", [False, True])
def test_transport_failure_before_or_after_output_never_replays(claude, partial):
    items = events()[:3] if partial else []
    provider, run, calls, body = streamed(
        claude, items, failure=httpx2.ReadError("private-stream-error")
    )
    iterator = provider.stream(REQUEST)
    if partial:
        assert next(iterator).content == "private-answer "
    with pytest.raises(ProviderError):
        next(iterator)
    assert len(calls) == 1
    assert body.closed
    assert run.budget.snapshot.blocked


@pytest.mark.parametrize(
    "edit",
    [
        "no_stop",
        "no_delta",
        "no_usage",
        "bad_usage",
        "unknown",
        "tool",
        "wrong_index",
        "decreasing",
        "unknown_usage",
    ],
)
def test_incomplete_malformed_or_unsupported_stream_blocks(claude, edit):
    items = events()
    if edit == "no_stop":
        items.pop()
    elif edit == "no_delta":
        items.pop(-2)
    elif edit == "no_usage":
        items[0]["message"]["usage"] = None
    elif edit == "bad_usage":
        items[-2]["usage"] = {"output_tokens": None}
    elif edit == "unknown":
        items.insert(2, {"type": "unknown_event"})
    elif edit == "tool":
        items[1]["content_block"] = {
            "type": "tool_use",
            "id": "x",
            "name": "tool",
            "input": {},
        }
    elif edit == "wrong_index":
        items[2]["index"] = 1
    elif edit == "decreasing":
        items[-2]["usage"]["output_tokens"] = 0
    elif edit == "unknown_usage":
        items[-2]["usage"]["new_billable_tokens"] = 1
    provider, run, calls, body = streamed(claude, items)
    with pytest.raises(ProviderError):
        list(provider.stream(REQUEST))
    assert body.closed
    assert run.budget.snapshot.blocked
    assert run.budget.snapshot.reserved_tokens == 0


def test_consumer_close_once_and_cancellation_close(claude):
    for cancel in (False, True):
        provider, run, calls, body = streamed(claude)
        iterator = provider.stream(REQUEST)
        next(iterator)
        if cancel:
            run.cancellation.cancel()
            with pytest.raises(OperationCancelled) as error:
                next(iterator)
            assert isinstance(error.value.__cause__, AccountingBlocked)
        else:
            with pytest.raises(AccountingBlocked):
                iterator.close()
        iterator.close()
        assert body.closed
        assert run.budget.snapshot.steps == 1
        assert run.budget.snapshot.committed_tokens == 132
        assert run.budget.snapshot.blocked


def test_usage_overrun_is_not_success(claude):
    items = events()
    items[-2]["usage"]["output_tokens"] = 200
    provider, run, calls, body = streamed(claude, items)
    with pytest.raises(AccountingBlocked):
        list(provider.stream(REQUEST))
    assert run.budget.snapshot.committed_tokens == 222
    assert body.closed


def test_incomplete_stop_with_valid_usage_is_charged(claude):
    provider, run, calls, body = streamed(
        claude, events(delta={"stop_reason": "max_tokens", "stop_sequence": None})
    )
    with pytest.raises(ProviderError, match="incomplete"):
        list(provider.stream(REQUEST))
    assert run.budget.snapshot.committed_tokens == 27
    assert not run.budget.snapshot.blocked
    assert body.closed


@pytest.mark.parametrize("complete", [False, True])
def test_cleanup_failure_is_sanitized_and_cannot_mask_failure(claude, complete):
    provider, run, calls, body = streamed(
        claude, events() if complete else events()[:3], cleanup_failure=True
    )
    with pytest.raises(ProviderError) as error:
        list(provider.stream(REQUEST))
    assert "private-cleanup-detail" not in str(error.value)
    assert run.budget.snapshot.blocked is not complete
    assert run.budget.snapshot.reserved_tokens == 0
    assert body.closed


def test_multiple_text_blocks_and_ping(claude):
    items = events(texts=("one",))
    items[-2:-2] = [
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": "two"},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    items.insert(2, {"type": "ping"})
    provider, run, calls, body = streamed(claude, items)
    assert [c.content for c in provider.stream(REQUEST)] == ["one", "two"]
    assert body.closed
