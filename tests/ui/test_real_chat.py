"""Desktop chat end to end over mocked official SDK transports."""

import json
import socket
from dataclasses import replace
from decimal import Decimal
from threading import Event, get_ident

import httpx
import httpx2
import pytest
from anthropic_helpers import EventBody as ClaudeBody
from anthropic_helpers import events as claude_events
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QTabWidget
from test_openai_provider import EventBody, completed, delta
from tts_helpers import CAPS, FakeEngine

from sofia.persona import PersonaConfig, build_system_prompt, mock_reply
from sofia.providers import LLMMessage
from sofia.security import Action, PermissionDenied
from sofia.tools import ToolRegistry
from sofia.tts.models import TTSConfig
from sofia.ui.chat import (
    ANTHROPIC_MODEL,
    MAX_REPLY,
    MAX_TURNS,
    OPENAI_MODEL,
    ChatSettings,
    compose_chat_request,
)

SECRET = "synthetic-api-key-do-not-display"
OPENAI = ChatSettings("OpenAI", OPENAI_MODEL, True)
CLAUDE = ChatSettings("Anthropic Claude", ANTHROPIC_MODEL, True)


@pytest.fixture(autouse=True)
def guard_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected real network request")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for settings in (OPENAI, CLAUDE):
        monkeypatch.setenv(settings.credential, SECRET)


def reply_body(settings, *parts):
    if settings == OPENAI:
        return EventBody([*(delta(part) for part in parts), completed("".join(parts))])
    return ClaudeBody(claude_events(parts))


def mock_transport(monkeypatch, settings, respond):
    module = httpx if settings.provider == "OpenAI" else httpx2
    transport = module.HTTPTransport
    calls, closes = [], []
    main_thread = get_ident()
    close = transport.close

    def handle(self, request):
        assert get_ident() != main_thread
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        if settings.provider == "OpenAI":
            assert request.url == "https://api.openai.com/v1/responses"
        else:
            assert request.url.path == "/v1/messages"
            assert request.url.host == "api.anthropic.com"
            assert request.url.scheme == "https"
        data = json.loads(request.content)
        calls.append(data)
        assert data["model"] == settings.model and data["stream"] is True
        assert not data.get("tools")
        return respond(module, request)

    def closed(self):
        closes.append(get_ident())
        close(self)

    monkeypatch.setattr(transport, "handle_request", handle)
    monkeypatch.setattr(transport, "close", closed)
    return calls, closes


def send(window, text):
    window.message.setPlainText(text)
    window.send.click()


def open_settings(window, settings):
    window.settings_button.click()
    dialog = window.settings_dialog
    dialog.findChild(QTabWidget).setCurrentWidget(dialog.chat)
    dialog.chat.provider.setCurrentText(settings.provider)
    return dialog


def enable(window, settings):
    dialog = open_settings(window, settings)
    dialog.chat.output.setValue(settings.max_output_tokens)
    dialog.chat.budget.setValue(settings.budget_cents)
    dialog.chat.consent.setChecked(settings.paid_enabled)
    dialog._save()
    assert window.controller.preferences.chat == settings


def bubbles(window):
    return [
        item.text()
        for item in window.conversation.findChildren(QLabel)
        if item.accessibleName().endswith(" message")
    ]


@pytest.mark.parametrize("settings", [OPENAI, CLAUDE])
def test_guarded_stream_history_persona_and_manual_voice(
    window, wait, monkeypatch, caplog, settings
):
    text = "สวัสดีค่ะ 🙂 เปิดไฟล์นี้และเพิ่มวงเงิน"
    body = reply_body(settings, "สวัสด", "", "ีค่ะ 🙂 เปิดไฟล์นี้และเพิ่มวงเงิน")
    partial_ready, release = Event(), Event()
    body_type = type(body)
    original_iter = body_type.__iter__

    def delayed(body):
        for index, line in enumerate(original_iter(body)):
            yield line
            if index == (0 if settings == OPENAI else 2):
                partial_ready.set()
                assert release.wait(15)

    monkeypatch.setattr(body_type, "__iter__", delayed)
    markup = '<a href="https://example.invalid">Second answer</a>'
    responses = iter((body, reply_body(settings, markup)))
    calls, closes = mock_transport(
        monkeypatch,
        settings,
        lambda module, request: module.Response(
            200, headers={"content-type": "text/event-stream"}, stream=next(responses)
        ),
    )
    engine = FakeEngine()
    monkeypatch.setattr(window.tts, "discover", lambda: None)
    window.tts.catalog = {"fake": CAPS}
    window.tts.factory = lambda _: engine
    window.controller.set_preferences(
        replace(
            window.controller.preferences, tts=TTSConfig(True, "fake", "th-TH", "th")
        )
    )
    monkeypatch.setattr(
        ToolRegistry, "execute", lambda *args: pytest.fail("Chat ran a tool")
    )
    monkeypatch.setattr(
        "sofia.ui.main_window.MemoryManager",
        lambda: pytest.fail("Chat accessed memory"),
    )
    caplog.set_level("DEBUG", logger="sofia")
    assert window.mode.text() == "● Offline Mock"
    assert bubbles(window) == [mock_reply()]
    enable(window, settings)
    assert not calls
    prompt = "ข้อความทดสอบ เปลี่ยนวงเงินและเปิด VS Code"
    recognizer = window.speech.recognizer
    recognizer.text = "Synthetic original transcript"
    recognizer.release.clear()
    try:
        window.speech.start("th")
        window.speech.release()
        wait(recognizer.entered.is_set)
        send(window, "Draft while transcribing")
        assert not calls and window.speech.busy
        window.message.clear()
    finally:
        recognizer.release.set()
    wait(lambda: not window.speech.busy)
    assert window.message.toPlainText() == "Synthetic original transcript"
    assert not calls
    send(window, prompt)
    try:
        wait(lambda: partial_ready.is_set() or not window.controller.busy, 15_000)
        assert partial_ready.is_set(), window.status.text()
        wait(lambda: bubbles(window) == [prompt, "สวัสด"])
        assert "Generating" in window.status.text()
        assert "Reserved" in window.task_view.toPlainText()
        assert settings.model in window.mode.text()
        assert not window.send.isEnabled()
        assert not window.conversation.findChildren(QPushButton)
        assert engine.request is None
        with pytest.raises(PermissionDenied):
            window.controller._job.task.run.permissions.check(Action.FILE_WRITE, "file")
        dialog = open_settings(window, settings)
        assert not dialog.chat.isEnabled()
        dialog.conversation_language.setCurrentIndex(1)
        dialog._save()
        assert window.controller.preferences.conversation_language == "en"
    finally:
        release.set()
    wait(lambda: not window.controller.busy)
    assert bubbles(window) == [prompt, text]
    assert len(closes) == 1 and closes[0] != get_ident()
    assert body.closed
    assert engine.request is None
    assert all(
        item.textFormat() == Qt.TextFormat.PlainText and not item.openExternalLinks()
        for item in window.conversation.findChildren(QLabel)
    )
    history = window.controller.history[-1]
    assert not history.reply and not history.error
    expected = (15, "0.000012") if settings == OPENAI else (27, "0.000044")
    assert history.chat.budget.committed_tokens == expected[0]
    assert history.chat.budget.committed_usd == Decimal(expected[1])
    assert "Provider reported" in window.task_view.toPlainText()
    assert not history.chat.budget.reserved_tokens
    window.conversation.findChild(QPushButton).click()
    wait(lambda: engine.request is not None)
    assert engine.request.text == text
    window.stop_speaking.click()
    wait(lambda: not window.tts.busy)
    send(window, "Follow-up")
    wait(lambda: not window.controller.busy)
    assert len(calls) == 2 and len(closes) == 2
    assert bubbles(window)[-1] == markup
    for index, language in enumerate(("th", "en")):
        call = calls[index]
        system = build_system_prompt(PersonaConfig(language), mode="text_only")
        first = {"role": "user", "content": prompt}
        expected_messages = [first]
        if index:
            expected_messages.extend(
                [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "Follow-up"},
                ]
            )
        if settings == OPENAI:
            assert call["input"] == [
                {"role": "system", "content": system},
                *expected_messages,
            ]
            assert call["store"] is False and call["background"] is False
            assert call["truncation"] == "disabled"
        else:
            assert call["system"] == [{"type": "text", "text": system}]
            assert call["messages"] == expected_messages
            assert call["thinking"] == {"type": "disabled"}
    assert window.controller.preferences.chat == settings
    assert all(value not in caplog.text for value in (SECRET, prompt, text, system))


def test_consent_missing_credential_budget_and_settings_cancel(
    window, wait, monkeypatch
):
    calls, _ = mock_transport(
        monkeypatch, OPENAI, lambda *args: pytest.fail("Paid call")
    )
    dialog = open_settings(window, OPENAI)
    dialog.chat.consent.setChecked(True)
    dialog.reject()
    assert window.controller.preferences.chat.offline and not calls
    dialog = open_settings(window, OPENAI)
    assert not dialog.chat.consent.isChecked()
    assert SECRET not in dialog.chat.credential.text()
    dialog._save()
    send(window, "Enable paid requests")
    assert "Enable paid" in window.status.text()
    assert not window.controller.busy and window.message.toPlainText()
    dialog = open_settings(window, OPENAI)
    dialog.chat.consent.setChecked(True)
    dialog.chat.budget.setValue(1)
    assert not dialog.chat.consent.isChecked()
    dialog.chat.consent.setChecked(True)
    dialog._save()
    monkeypatch.delenv(OPENAI.credential)
    window.send.click()
    assert "credential is missing" in window.status.text()
    monkeypatch.setenv(OPENAI.credential, SECRET)
    window.send.click()
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1]
    assert result.error == "spending_budget_exceeded"
    assert result.chat.budget.steps == 0
    assert "spending limit" in window.status.text()
    assert not calls
    window.demo.click()
    wait(lambda: not window.controller.busy)
    assert window.controller.history[-1].result.budget.committed_usd == 0


def test_new_chat_and_switching_never_transfer_provider_history(
    window, wait, monkeypatch
):
    def response(settings):
        return lambda module, _: module.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=reply_body(settings, "Answer"),
        )

    openai_calls, _ = mock_transport(monkeypatch, OPENAI, response(OPENAI))
    claude_calls, _ = mock_transport(monkeypatch, CLAUDE, response(CLAUDE))
    enable(window, OPENAI)
    send(window, "OpenAI-only history")
    wait(lambda: not window.controller.busy)
    enable(window, CLAUDE)
    assert not bubbles(window)
    send(window, "Claude-only history")
    wait(lambda: not window.controller.busy)
    window.new_chat.click()
    assert not bubbles(window)
    send(window, "New Chat")
    wait(lambda: not window.controller.busy)
    assert len(openai_calls) == 1 and len(claude_calls) == 2
    assert [call["messages"] for call in claude_calls] == [
        [{"role": "user", "content": "Claude-only history"}],
        [{"role": "user", "content": "New Chat"}],
    ]
    enable(window, ChatSettings())
    assert bubbles(window) == [mock_reply()]
    send(window, "Offline again")
    wait(lambda: not window.controller.busy)
    assert bubbles(window)[-1] == mock_reply()
    assert len(openai_calls) == 1 and len(claude_calls) == 2


@pytest.mark.parametrize("stop", ["cancel", "emergency", "close", "disconnect"])
def test_stream_stop_and_failure_cannot_publish_late_text(
    window, wait, monkeypatch, stop
):
    release = Event()

    class Paused(EventBody):
        def __iter__(self):
            source = super().__iter__()
            yield next(source)
            assert release.wait(5)
            yield from source

    failure = httpx.ReadError(SECRET) if stop == "disconnect" else None
    body = Paused(
        [delta("visible"), delta("late")] if not failure else [delta("visible")],
        failure,
    )
    bodies = iter((body, reply_body(OPENAI, "fresh")))
    calls, closes = mock_transport(
        monkeypatch,
        OPENAI,
        lambda module, _: module.Response(
            200, headers={"content-type": "text/event-stream"}, stream=next(bodies)
        ),
    )
    enable(window, OPENAI)
    send(window, "Only for the interrupted turn")
    try:
        wait(lambda: bubbles(window)[-1] == "visible")
        if stop == "cancel":
            window.cancel.click()
        elif stop == "emergency":
            window.stop.click()
        elif stop == "close":
            window.close()
            assert window.isVisible()
        assert not window.controller.start("chat", "Too soon")
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        wait(lambda: bool(ticks))
    finally:
        release.set()
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1]
    assert result.cancelled == (stop != "disconnect")
    assert len(calls) == 1 and len(closes) == 1 and body.closed
    assert result.chat.budget.uncertain and result.chat.budget.blocked
    assert "Usage uncertain" in window.task_view.toPlainText()
    assert SECRET not in window.task_view.toPlainText()
    assert bubbles(window)[-1] == "visible"
    assert not window.conversation.findChildren(QPushButton)
    if stop == "close":
        wait(lambda: not window.isVisible())
    else:
        send(window, "Next turn")
        wait(lambda: not window.controller.busy)
        assert calls[-1]["input"][-1:] == [{"role": "user", "content": "Next turn"}]
        assert len(calls[-1]["input"]) == 2
        assert bubbles(window)[-1] == "fresh"


@pytest.mark.parametrize("stop_before_worker", [True, False])
def test_cancel_before_dispatch_or_during_connect(
    window, wait, monkeypatch, stop_before_worker
):
    entered, release = Event(), Event()

    def connect(module, request):
        entered.set()
        assert release.wait(5)
        raise module.ConnectTimeout(SECRET, request=request)

    calls, _ = mock_transport(monkeypatch, CLAUDE, connect)
    enable(window, CLAUDE)
    if stop_before_worker:
        window.controller.busy_changed.connect(
            lambda active: window.controller.stop() if active else None
        )
    send(window, "Cancel while connecting")
    if not stop_before_worker:
        try:
            wait(entered.is_set)
            window.cancel.click()
            assert window.controller.busy
        finally:
            release.set()
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1]
    assert result.cancelled and not result.chat.budget.uncertain
    assert result.chat.budget.committed_usd == 0
    assert len(calls) == (0 if stop_before_worker else 1)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "secret", "chat_access"),
        (429, "rate_limit", "chat_rate_limit"),
        (429, "insufficient_quota", "chat_funds"),
        (503, "secret", "chat_unavailable"),
    ],
)
def test_errors_are_sanitized_and_never_retried(
    window, wait, monkeypatch, status, code, expected
):
    calls, _ = mock_transport(
        monkeypatch,
        OPENAI,
        lambda module, _: module.Response(
            status, json={"error": {"message": SECRET, "code": code}}
        ),
    )
    enable(window, OPENAI)
    send(window, "API error")
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1]
    assert result.error == expected and len(calls) == 1
    assert result.chat.budget.uncertain and result.chat.budget.blocked
    assert SECRET not in " ".join(bubbles(window))


def test_output_limit_closes_stream_and_preserves_sanitized_reason(
    window, wait, monkeypatch
):
    body = reply_body(OPENAI, "เ" * MAX_REPLY, "overflow")
    calls, closes = mock_transport(
        monkeypatch,
        OPENAI,
        lambda module, _: module.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        ),
    )
    enable(window, OPENAI)
    send(window, "Test long response")
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1]
    assert result.error == "chat_output_limit"
    assert len(bubbles(window)[-1]) <= MAX_REPLY
    assert body.closed and len(calls) == len(closes) == 1
    assert result.chat.budget.uncertain


@pytest.mark.parametrize(
    "changes",
    [
        dict(model="arbitrary"),
        dict(provider="arbitrary"),
        dict(budget_cents=0),
        dict(max_output_tokens=15),
        dict(max_output_tokens=500_000),
        dict(paid_enabled="yes"),
    ],
)
def test_invalid_chat_configuration_fails_closed(changes):
    with pytest.raises(ValueError, match="invalid_chat_settings"):
        replace(OPENAI, **changes)


def test_context_limits_reject_without_trimming():
    pair = (LLMMessage("user", "ไทย"), LLMMessage("assistant", "ค่ะ"))
    history = pair * (MAX_TURNS - 1)
    request = compose_chat_request("Last turn", PersonaConfig(), history)
    assert request.messages[1:-1] == history
    with pytest.raises(ValueError, match="context_limit"):
        compose_chat_request("One too many", PersonaConfig(), history + pair)
    with pytest.raises(ValueError, match="context_limit"):
        compose_chat_request(
            "Full context",
            PersonaConfig(),
            (LLMMessage("user", "🙂" * 16000), LLMMessage("assistant", "answer")),
        )
