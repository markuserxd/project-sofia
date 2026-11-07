import logging
import socket
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from sofia.config import load_settings
from sofia.providers import (
    InvalidRequestError,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
    MockFailure,
    MockLLMProvider,
    ProviderError,
    TemporaryProviderError,
)


@pytest.fixture
def conversation() -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content="Be helpful."),
            LLMMessage(role="user", content="Hello"),
            LLMMessage(role="assistant", content="Hi"),
            LLMMessage(role="user", content="Continue"),
        )
    )


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
def test_valid_messages(role: MessageRole) -> None:
    message = LLMMessage(role=role, content="")
    assert message.role == role
    assert message.content == ""
    assert LLMRequest(messages=(message,)).messages == (message,)


@pytest.mark.parametrize("role", ["tool", "USER", "", None, [], 1])
def test_invalid_roles(role: object) -> None:
    with pytest.raises(InvalidRequestError, match="^Message role must be"):
        LLMMessage(role=cast(MessageRole, role), content="text")


@pytest.mark.parametrize("content", [None, 42, b"text", ["text"]])
def test_non_text_content(content: object) -> None:
    with pytest.raises(InvalidRequestError, match="^Message content must be text"):
        LLMMessage(role="user", content=cast(str, content))
    for model in (LLMResponse, LLMStreamChunk):
        with pytest.raises(InvalidRequestError, match="content must be text"):
            model(content=cast(str, content))


@pytest.mark.parametrize("messages", [(), [], None, "text", ("text",), (None,)])
def test_invalid_conversations(messages: object) -> None:
    with pytest.raises(InvalidRequestError, match="^Request messages must be"):
        LLMRequest(messages=cast(tuple[LLMMessage, ...], messages))


def test_mutable_message_list_is_rejected(conversation: LLMRequest) -> None:
    with pytest.raises(InvalidRequestError):
        LLMRequest(messages=cast(tuple[LLMMessage, ...], list(conversation.messages)))


def test_models_are_immutable(conversation: LLMRequest) -> None:
    for model, field, value in (
        (conversation.messages[0], "content", "changed"),
        (conversation.messages[0], "role", "user"),
        (conversation, "messages", ()),
        (LLMResponse("answer"), "content", "changed"),
        (LLMStreamChunk("part"), "content", "changed"),
        (MockLLMProvider(), "response_text", "changed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(model, field, value)


def test_default_provider_satisfies_protocol(conversation: LLMRequest) -> None:
    # This assignment also checks structural compatibility when type-checked.
    provider: LLMProvider = MockLLMProvider()
    assert provider.generate(conversation) == LLMResponse(content="Hello from SOFIA")
    assert "".join(chunk.content for chunk in provider.stream(conversation)) == (
        "Hello from SOFIA"
    )
    assert load_settings({}).provider == "mock"


@pytest.mark.parametrize("text", ["", "a", "Hello from SOFIA", "a\n b\t🙂ไทย"])
@pytest.mark.parametrize("size", [1, 6, 100])
def test_deterministic_generation_and_streaming(
    conversation: LLMRequest, text: str, size: int
) -> None:
    provider = MockLLMProvider(response_text=text, chunk_size=size)
    expected = [
        LLMStreamChunk(text[start : start + size])
        for start in range(0, len(text), size)
    ]
    for _ in range(2):
        assert provider.generate(conversation) == LLMResponse(text)
        assert list(provider.stream(conversation)) == expected
        assert "".join(chunk.content for chunk in provider.stream(conversation)) == text
    assert list(MockLLMProvider(response_text="").stream(conversation)) == []


def test_streams_have_independent_progress(conversation: LLMRequest) -> None:
    provider = MockLLMProvider(response_text="abcdef", chunk_size=2)
    first, second = provider.stream(conversation), provider.stream(conversation)
    assert next(first) == next(second) == LLMStreamChunk("ab")
    assert provider.generate(conversation).content == "abcdef"
    assert list(first) == list(second) == [LLMStreamChunk("cd"), LLMStreamChunk("ef")]


@pytest.mark.parametrize(
    ("failure", "error_type", "message"),
    [
        ("general", ProviderError, "Simulated mock provider failure."),
        (
            "temporary",
            TemporaryProviderError,
            "Simulated temporary mock provider failure.",
        ),
        (
            "invalid_request",
            InvalidRequestError,
            "Simulated unsupported mock provider request.",
        ),
    ],
)
@pytest.mark.parametrize("after", [0, 1, 3, 10])
@pytest.mark.parametrize("text", ["", "abcdef"])
def test_simulated_failures(
    conversation: LLMRequest,
    failure: MockFailure,
    error_type: type[ProviderError],
    message: str,
    after: int,
    text: str,
) -> None:
    provider = MockLLMProvider(
        response_text=text, chunk_size=2, failure=failure, stream_fail_after=after
    )
    for _ in range(2):
        with pytest.raises(error_type) as error:
            provider.generate(conversation)
        assert type(error.value) is error_type
        assert str(error.value) == message
        stream = provider.stream(conversation)  # Creating the iterator does not raise.
        expected = [text[start : start + 2] for start in range(0, len(text), 2)]
        assert [next(stream).content for _ in expected[:after]] == expected[:after]
        with pytest.raises(error_type) as error:
            next(stream)
        assert type(error.value) is error_type
        assert str(error.value) == message


@pytest.mark.parametrize(
    "value", [None, "private-input", (), {"secret": "private-input"}]
)
def test_invalid_request_at_provider_boundary(value: object) -> None:
    provider = MockLLMProvider()
    with pytest.raises(InvalidRequestError, match="^Expected an LLMRequest object"):
        provider.generate(cast(LLMRequest, value))
    stream = provider.stream(cast(LLMRequest, value))
    with pytest.raises(InvalidRequestError, match="^Expected an LLMRequest object"):
        next(stream)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_text", None),
        ("chunk_size", 0),
        ("chunk_size", -1),
        ("chunk_size", True),
        ("chunk_size", 1.5),
        ("chunk_size", "private-input"),
        ("failure", "private-input"),
        ("failure", []),
        ("stream_fail_after", -1),
        ("stream_fail_after", True),
        ("stream_fail_after", 1.5),
        ("stream_fail_after", "private-input"),
    ],
)
def test_invalid_mock_configuration(field: str, value: object) -> None:
    with pytest.raises(ValueError) as error:
        MockLLMProvider(**{field: value})  # type: ignore[arg-type]
    assert "private-input" not in str(error.value)


def test_offline_without_credentials_or_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbid_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Provider attempted network access")

    monkeypatch.setattr(socket, "socket", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbid_network)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRIVATE_TEST_VALUE", "private-environment")
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="sofia")
    conversation = LLMRequest((LLMMessage("user", "private-prompt"),))
    for failure in (None, "general", "temporary", "invalid_request"):
        provider = MockLLMProvider(
            response_text="private-response", failure=cast(MockFailure | None, failure)
        )
        if failure is None:
            assert provider.generate(conversation).content == "private-response"
            assert "".join(
                chunk.content for chunk in provider.stream(conversation)
            ) == ("private-response")
        else:
            with pytest.raises(ProviderError) as error:
                provider.generate(conversation)
            with pytest.raises(ProviderError) as stream_error:
                list(provider.stream(conversation))
            for private in (
                "private-prompt",
                "private-response",
                "private-environment",
            ):
                assert private not in str(error.value)
                assert private not in str(stream_error.value)
    assert caplog.records == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
