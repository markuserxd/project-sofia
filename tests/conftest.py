"""Offline provider fixtures. All test-process socket access is forbidden."""

import socket
from decimal import Decimal

import anthropic
import httpx2
import pytest
from anthropic_helpers import configuration, payload

from sofia.providers.anthropic_provider import AnthropicProvider
from sofia.security import Action, RunGuardrails


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected real network activity")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def claude():
    clients = []

    def make(handler=None, *, config=None, run=None, approve=True, **limits):
        if run is None:
            run = RunGuardrails(
                **(
                    {"max_steps": 10, "max_tokens": 1000, "max_usd": Decimal("1")}
                    | limits
                )
            )
        calls = []

        def transport(request):
            calls.append(request)
            return (
                handler(request, run)
                if handler
                else httpx2.Response(200, json=payload())
            )

        client = anthropic.Anthropic(
            api_key="fake-private-key",
            max_retries=2,
            http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
        )
        clients.append(client)
        provider = AnthropicProvider(
            config=config or configuration(), run=run, client=client
        )
        if approve:
            run.permissions.approve(Action.API_REQUEST, provider.permission_scope)
        return provider, run, calls

    yield make
    for client in clients:
        client.close()
