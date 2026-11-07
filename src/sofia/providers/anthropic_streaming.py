"""Strict text-only state machine for raw Messages SSE events."""

from typing import Any

from sofia.providers.anthropic_accounting import AnthropicUsage, count, fields
from sofia.providers.base import ProviderError


class TextStream:
    def __init__(self) -> None:
        self.message: dict[str, Any] | None = None
        self.blocks: list[str] = []
        self.active: int | None = None
        self.delta_seen = False
        self.stopped = False

    def accept(self, event: Any) -> str | None:
        kind = event.type
        if self.stopped:
            raise ProviderError("Anthropic stream continued after completion.")
        if kind == "ping":
            return None
        if kind == "message_start" and self.message is None:
            self.message = fields(event.message)
            if (
                self.message.get("type") != "message"
                or self.message.get("role") != "assistant"
                or self.message.get("content") != []
                or self.message.get("stop_reason") is not None
            ):
                raise ProviderError("Anthropic stream start was malformed.")
            self.message["usage"] = fields(self.message.get("usage"))
            AnthropicUsage.from_sdk(self.message["usage"])
            return None
        if self.message is None:
            raise ProviderError("Anthropic stream is missing its message start.")
        if (
            kind == "content_block_start"
            and self.active is None
            and not self.delta_seen
        ):
            index = count(event.index)
            block = fields(event.content_block)
            if (
                index != len(self.blocks)
                or block.get("type") != "text"
                or type(block.get("text")) is not str
                or block.get("citations")
            ):
                raise ProviderError("Anthropic stream returned unsupported content.")
            self.active = index
            self.blocks.append(block["text"])
            return block["text"] if block["text"] else None
        if kind == "content_block_delta" and self.active is not None:
            if (
                count(event.index) != self.active
                or event.delta.type != "text_delta"
                or type(event.delta.text) is not str
            ):
                raise ProviderError("Anthropic stream returned an invalid text delta.")
            self.blocks[self.active] += event.delta.text
            return event.delta.text
        if kind == "content_block_stop" and self.active is not None:
            if count(event.index) != self.active:
                raise ProviderError("Anthropic stream block order is invalid.")
            self.active = None
            return None
        if kind == "message_delta" and self.active is None:
            delta = fields(event.delta)
            if (
                delta.keys()
                - {"stop_reason", "stop_sequence", "container", "stop_details"}
                or delta.get("container") is not None
            ):
                raise ProviderError(
                    "Anthropic stream returned unsupported message metadata."
                )
            self.message.update(delta)
            usage = fields(event.usage)
            # Delta totals are cumulative, not additive. Only documented optional
            # fields may be omitted; output_tokens is mandatory in every delta.
            count(usage.get("output_tokens"))
            if usage.keys() - {
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "server_tool_use",
                "output_tokens_details",
            }:
                raise ProviderError("Anthropic stream returned unknown billable usage.")
            previous = self.message["usage"]
            for name, value in usage.items():
                if value is not None:
                    if name.endswith("tokens"):
                        current = count(value)
                        old = previous.get(name)
                        if old is not None and current < count(old):
                            raise ProviderError("Anthropic stream usage decreased.")
                    previous[name] = value
            AnthropicUsage.from_sdk(previous)
            self.delta_seen = True
            return None
        if kind == "message_stop" and self.active is None and self.delta_seen:
            self.message["content"] = [
                {"type": "text", "text": text} for text in self.blocks
            ]
            self.stopped = True
            return None
        raise ProviderError(
            "Anthropic stream failed or returned an unsupported event order."
        )
