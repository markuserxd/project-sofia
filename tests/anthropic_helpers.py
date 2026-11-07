"""Real SDK fixtures over a deterministic, in-memory HTTP transport."""

import json
from dataclasses import replace

import httpx2

from sofia.providers import LLMMessage, LLMRequest
from sofia.providers.anthropic_provider import AnthropicConfig

MODEL = "claude-haiku-4-5-20251001"
REQUEST = LLMRequest(
    (
        LLMMessage("system", "private-system"),
        LLMMessage("system", "second instruction"),
        LLMMessage("user", "private-prompt ไทย🙂"),
        LLMMessage("assistant", "history"),
        LLMMessage("user", "next question"),
    )
)


def configuration(**changes):
    return replace(
        AnthropicConfig(
            model=MODEL, input_token_bound=lambda request: 100, max_output_tokens=32
        ),
        **changes,
    )


def usage(**changes):
    return {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 5,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 3,
            "ephemeral_1h_input_tokens": 2,
        },
        "service_tier": "standard",
        "inference_geo": "global",
        "speed": "standard",
    } | changes


def payload(text="private-answer ไทย🙂", **changes):
    return {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage(),
    } | changes


def events(texts=("private-answer ", "", "ไทย🙂"), **changes):
    start = payload(content=[], stop_reason=None)
    start["usage"]["output_tokens"] = 1
    return [
        {"type": "message_start", "message": start},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        *[
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": t},
            }
            for t in texts
        ],
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
            **changes,
        },
        {"type": "message_stop"},
    ]


class EventBody(httpx2.SyncByteStream):
    def __init__(self, items, failure=None, cleanup_failure=False):
        self.items = items
        self.failure = failure
        self.cleanup_failure = cleanup_failure
        self.closed = False

    def __iter__(self):
        for event in self.items:
            yield (
                "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
            ).encode()
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True
        if self.cleanup_failure:
            raise RuntimeError("private-cleanup-detail")
