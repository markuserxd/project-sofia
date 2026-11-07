import json
import logging
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from unittest.mock import Mock

import anthropic
import httpx2
import pytest
from anthropic_helpers import MODEL, REQUEST, configuration, payload, usage

from sofia.config import ConfigurationError
from sofia.providers import (
    InvalidRequestError,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    TemporaryProviderError,
)
from sofia.providers.anthropic_provider import AnthropicProvider
from sofia.providers.base import UndispatchedProviderError
from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    SpendingBudgetExceeded,
    StepLimitExceeded,
    TokenBudgetExceeded,
)


def test_real_sdk_conversion_and_reservation_precedes_dispatch(claude):
    def handler(request, run):
        assert run.budget.snapshot.reserved_tokens == 132
        assert run.budget.snapshot.reserved_usd == Decimal("0.000360")
        assert request.extensions["timeout"] == {
            "connect": 5.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
        return httpx2.Response(200, json=payload())

    provider, run, calls = claude(handler)
    typed: LLMProvider = provider
    assert calls == []
    assert typed.generate(REQUEST) == LLMResponse("private-answer ไทย🙂")
    assert json.loads(calls[0].content) == {
        "model": MODEL,
        "max_tokens": 32,
        "stream": False,
        "service_tier": "standard_only",
        "thinking": {"type": "disabled"},
        "system": [{"type": "text", "text": m.content} for m in REQUEST.messages[:2]],
        "messages": [
            {"role": m.role, "content": m.content} for m in REQUEST.messages[2:]
        ],
    }
    assert run.budget.snapshot.committed_tokens == 27
    assert run.budget.snapshot.committed_usd == Decimal("0.000044")
    assert run.budget.snapshot.reserved_tokens == 0
    assert not run.budget.snapshot.blocked


@pytest.mark.parametrize("stream", [False, True])
def test_permission_wrong_scope_revocation_and_stop(claude, stream):
    provider, run, calls = claude(approve=False)

    def invoke():
        return list(provider.stream(REQUEST)) if stream else provider.generate(REQUEST)

    with pytest.raises(PermissionDenied):
        invoke()
    run.permissions.approve(Action.API_REQUEST, "anthropic:wrong-model")
    with pytest.raises(PermissionDenied):
        invoke()
    run.permissions.approve(Action.API_REQUEST, provider.permission_scope)
    run.permissions.revoke(Action.API_REQUEST, provider.permission_scope)
    with pytest.raises(PermissionDenied):
        invoke()
    run.cancellation.cancel()
    with pytest.raises(OperationCancelled):
        invoke()
    assert calls == []
    assert run.budget.snapshot.steps == 0


@pytest.mark.parametrize(
    "limits,error",
    [
        ({"max_tokens": 131}, TokenBudgetExceeded),
        ({"max_usd": Decimal("0.000359")}, SpendingBudgetExceeded),
        ({"max_steps": 0}, StepLimitExceeded),
    ],
)
def test_budget_denies_without_dispatch(claude, limits, error):
    provider, run, calls = claude(**limits)
    with pytest.raises(error):
        provider.generate(REQUEST)
    assert calls == []


@pytest.mark.parametrize(
    "messages",
    [
        None,
        "private-prompt",
        (),
        (LLMMessage("user", "first"), LLMMessage("system", "late")),
        (LLMMessage("system", "only"),),
        (LLMMessage("assistant", "prefill"),),
        (LLMMessage("user", "one"), LLMMessage("user", "two")),
        (LLMMessage("user", "one"), LLMMessage("assistant", "prefill")),
    ],
)
def test_unrepresentable_conversations_rejected(claude, messages):
    request = (
        LLMRequest(messages) if messages and isinstance(messages, tuple) else messages
    )
    provider, run, calls = claude()
    with pytest.raises(InvalidRequestError):
        provider.generate(request)
    with pytest.raises(InvalidRequestError):
        next(provider.stream(request))
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "unknown"),
        ("model", "private\nmodel"),
        ("max_output_tokens", 0),
        ("max_output_tokens", True),
        ("max_output_tokens", 64001),
        ("input_token_bound", None),
        ("io_timeout", 0),
        ("io_timeout", -1),
        ("io_timeout", float("nan")),
        ("io_timeout", float("inf")),
        ("connect_timeout", True),
        ("io_timeout", "30"),
        ("io_timeout", 10**400),
    ],
)
def test_config_validation(field, value):
    with pytest.raises(ConfigurationError):
        configuration(**{field: value})


@pytest.mark.parametrize("bound", [0, -1, True, 1.5, None, "private", 200001])
def test_bad_bound_rejected_before_dispatch(claude, bound):
    provider, run, calls = claude(
        config=configuration(input_token_bound=lambda request: bound)
    )
    with pytest.raises(InvalidRequestError):
        provider.generate(REQUEST)
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"usage": None},
        {"model": "wrong"},
        {"usage": usage(new_billable_tokens=1)},
        {"usage": usage(output_tokens=-1)},
        {"usage": usage(cache_creation=None)},
    ],
)
def test_uncertain_accounting_commits_reservation_once_and_blocks(claude, changes):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(200, json=payload(**changes))
    )
    with pytest.raises(ProviderError) as error:
        provider.generate(REQUEST)
    assert isinstance(error.value.__cause__, AccountingBlocked)
    assert run.budget.snapshot.blocked
    assert run.budget.snapshot.committed_tokens == 132
    assert run.budget.snapshot.committed_usd == Decimal("0.000360")
    assert run.budget.snapshot.reserved_tokens == 0
    with pytest.raises(AccountingBlocked):
        provider.generate(REQUEST)
    assert len(calls) == 1


def test_overrun_records_all_actual_usage(claude):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(
            200, json=payload(usage=usage(input_tokens=200))
        )
    )
    with pytest.raises(AccountingBlocked):
        provider.generate(REQUEST)
    assert run.budget.snapshot.committed_tokens == 217
    assert run.budget.snapshot.blocked


@pytest.mark.parametrize(
    "reported,total", [({"input_tokens": 90}, 107), ({"output_tokens": 33}, 55)]
)
def test_individual_cap_breach_blocks_even_below_aggregate_reservation(
    claude, reported, total
):
    # Cached input also belongs in the input bound: 90+7+5=102 > 100.
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(200, json=payload(usage=usage(**reported)))
    )
    with pytest.raises(AccountingBlocked):
        provider.generate(REQUEST)
    assert run.budget.snapshot.committed_tokens == total < 132
    assert run.budget.snapshot.committed_usd < Decimal("0.000360")
    assert run.budget.snapshot.blocked
    assert run.budget.snapshot.reserved_tokens == 0


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, InvalidRequestError),
        (401, ProviderError),
        (403, ProviderError),
        (404, InvalidRequestError),
        (422, InvalidRequestError),
        (429, TemporaryProviderError),
        (500, TemporaryProviderError),
    ],
)
def test_safe_sdk_errors_and_no_automatic_retries(claude, status, expected):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(
            status,
            json={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "fake-private-key private-prompt",
                },
            },
        )
    )
    with pytest.raises(expected) as error:
        provider.generate(REQUEST)
    assert "fake-private-key" not in "".join(traceback.format_exception(error.value))
    assert len(calls) == 1
    assert run.budget.snapshot.blocked


@pytest.mark.parametrize(
    "failure,safe",
    [
        (httpx2.ConnectError, True),
        (httpx2.ConnectTimeout, True),
        (httpx2.PoolTimeout, True),
        (httpx2.ReadTimeout, False),
        (httpx2.WriteError, False),
        (httpx2.ReadError, False),
    ],
)
def test_transport_evidence_and_no_replay(claude, failure, safe):
    def handler(req, run):
        raise failure("private-error", request=req)

    provider, run, calls = claude(handler)
    with pytest.raises(UndispatchedProviderError if safe else TemporaryProviderError):
        provider.generate(REQUEST)
    assert run.budget.snapshot.blocked is not safe
    assert run.budget.snapshot.committed_tokens == (0 if safe else 132)
    assert len(calls) == run.budget.snapshot.steps == 1


def test_cancellation_releases_pre_dispatch_reservation(claude, monkeypatch):
    provider, run, calls = claude()
    original = run.begin_request

    def reserve_then_stop(*args, **kwargs):
        reservation = original(*args, **kwargs)
        run.cancellation.cancel()
        return reservation

    monkeypatch.setattr(run, "begin_request", reserve_then_stop)
    with pytest.raises(OperationCancelled):
        provider.generate(REQUEST)
    assert calls == []
    assert (
        run.budget.snapshot.committed_tokens == run.budget.snapshot.reserved_tokens == 0
    )


def test_stop_after_blocking_generate_preserves_trusted_usage(claude):
    def handler(req, run):
        run.cancellation.cancel()
        return httpx2.Response(200, json=payload())

    provider, run, calls = claude(handler)
    with pytest.raises(OperationCancelled):
        provider.generate(REQUEST)
    assert run.budget.snapshot.committed_tokens == 27
    assert not run.budget.snapshot.blocked


def test_concurrent_attempts_cannot_oversubscribe(claude):
    admitted, release = Event(), Event()

    def handler(req, run):
        admitted.set()
        assert release.wait(5)
        return httpx2.Response(200, json=payload())

    provider, run, calls = claude(handler, max_tokens=132)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider.generate, REQUEST)
        assert admitted.wait(5)
        try:
            with pytest.raises(TokenBudgetExceeded):
                provider.generate(REQUEST)
        finally:
            release.set()
        assert first.result(5).content == "private-answer ไทย🙂"
    assert len(calls) == 1


def test_initialization_credentials_endpoint_and_client_ownership(claude, monkeypatch):
    run = RunGuardrails(max_steps=1, max_tokens=1000, max_usd=Decimal(1))
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(config=configuration(), run=run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-private-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "nonexistent-profile")
    provider = AnthropicProvider(config=configuration(), run=run)
    assert str(provider._client.base_url) == "https://api.anthropic.com"
    assert provider._client.max_retries == 0
    assert "fake-private-key" not in repr(provider)
    assert not provider._client.open_telemetry["traces"]["enabled"]
    provider.close()
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: private")
    with pytest.raises(ConfigurationError, match="custom headers"):
        AnthropicProvider(config=configuration(), run=run)


def test_fake_client_requires_no_credentials_and_stays_caller_owned():
    client = Mock()
    client.with_options.return_value = client
    client.messages.create.return_value = anthropic.types.Message.model_construct(
        **payload()
    )
    run = RunGuardrails(max_steps=1, max_tokens=1000, max_usd=Decimal(1))
    provider = AnthropicProvider(config=configuration(), run=run, client=client)
    run.permissions.approve(Action.API_REQUEST, provider.permission_scope)
    assert provider.generate(REQUEST).content == "private-answer ไทย🙂"
    provider.close()
    client.close.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_reason": "max_tokens"},
        {"stop_reason": "refusal"},
        {
            "content": [
                {"type": "tool_use", "id": "tool", "name": "private", "input": {}}
            ]
        },
        {"content": None},
    ],
)
def test_unsuccessful_content_is_still_accounted(claude, changes):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(200, json=payload(**changes))
    )
    with pytest.raises(ProviderError):
        provider.generate(REQUEST)
    assert run.budget.snapshot.committed_tokens == 27
    assert not run.budget.snapshot.blocked


def test_privacy_scoped_sdk_logging_and_logging_failure(claude, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    provider, run, calls = claude()
    provider.generate(REQUEST)
    logger = logging.getLogger("httpx2")
    logger.info("unrelated-client-metadata")
    assert "unrelated-client-metadata" in caplog.text
    assert "anthropic_usage input=10 output=5" in caplog.text
    for private in (
        "fake-private-key",
        "private-prompt",
        "private-system",
        "private-answer",
    ):
        assert private not in caplog.text

    def broken(*args, **kwargs):
        raise RuntimeError("private-log-error")

    monkeypatch.setattr(logging.getLogger("sofia.providers.anthropic"), "info", broken)
    provider.generate(REQUEST)
    assert run.budget.snapshot.committed_tokens == 54


def test_default_imports_and_routing_do_not_require_either_sdk():
    program = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('anthropic', 'openai', 'httpx', 'httpx2'):
            raise AssertionError('optional SDK imported')
sys.meta_path.insert(0, Block())
import sofia.providers
import sofia.providers.routing
from sofia.cli import main
assert main() == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "sdk,blocked", [("anthropic", "openai"), ("openai", "anthropic")]
)
def test_each_optional_adapter_imports_without_the_other(sdk, blocked):
    program = f"""
import importlib.abc, importlib, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] == {blocked!r}:
            raise AssertionError('other provider SDK imported')
sys.meta_path.insert(0, Block())
importlib.import_module('sofia.providers.' + {sdk!r} + '_provider')
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_malformed_sdk_values_and_http_errors_do_not_leak_through_warnings_or_logs(
    claude, caplog, recwarn, capsys
):
    caplog.set_level(logging.DEBUG)
    provider, run, _ = claude(
        lambda req, run: httpx2.Response(
            200, json=payload(usage=usage(output_tokens="private-secret-token"))
        )
    )
    with pytest.raises(ProviderError) as error:
        provider.generate(REQUEST)
    assert "private-secret-token" not in "".join(
        traceback.format_exception(error.value)
    )
    provider, run, _ = claude(
        lambda req, run: httpx2.Response(
            500,
            json={"error": {"type": "api_error", "message": "private-upstream-body"}},
        )
    )
    with pytest.raises(ProviderError):
        provider.generate(REQUEST)
    assert not recwarn.list
    assert "private-secret-token" not in caplog.text + capsys.readouterr().err
    assert "private-upstream-body" not in caplog.text


@pytest.mark.parametrize(
    "changes,blocked",
    [
        ({"stop_details": {"type": "refusal", "explanation": "private"}}, False),
        (
            {
                "container": {
                    "id": "container_fixture",
                    "expires_at": "2026-11-07T00:00:00Z",
                }
            },
            True,
        ),
    ],
)
def test_refusal_details_and_container_never_become_success(claude, changes, blocked):
    provider, run, _ = claude(
        lambda req, run: httpx2.Response(200, json=payload(**changes))
    )
    with pytest.raises(ProviderError):
        provider.generate(REQUEST)
    assert run.budget.snapshot.blocked is blocked
