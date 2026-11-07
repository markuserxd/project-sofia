import builtins
import json
import logging
import socket
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from sofia.config import Settings, load_settings
from sofia.logging_setup import configure_logging
from sofia.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    MockLLMProvider,
    ProviderError,
)
from sofia.security import (
    AccountingBlocked,
    Action,
    InvalidGuardrailInput,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    StepLimitExceeded,
    UnknownAction,
)


def run_context() -> RunGuardrails:
    return RunGuardrails(max_steps=2, max_tokens=10, max_usd=Decimal("0.2"))


def test_guarded_steps_cannot_bypass_permission_or_paid_preflight() -> None:
    run = run_context()
    with pytest.raises(PermissionDenied):
        run.begin_step(Action.FILE_WRITE, "resource")
    with pytest.raises(PermissionDenied):
        run.begin_request("model", estimated_tokens=10, estimated_usd=Decimal("0.1"))
    assert run.budget.snapshot.steps == 0
    run.permissions.approve(Action.API_REQUEST, "model")
    with pytest.raises(InvalidGuardrailInput):
        run.begin_step(Action.API_REQUEST, "model")
    for value in (None, 0.1):
        with pytest.raises(InvalidGuardrailInput):
            run.begin_request(
                "model", estimated_tokens=1, estimated_usd=cast(Decimal, value)
            )
    assert run.budget.snapshot.steps == 0


def test_shared_context_across_child_operations_and_isolated_runs() -> None:
    run, other = run_context(), run_context()
    run.permissions.approve(Action.FILE_WRITE, "resource")

    def child(context: RunGuardrails) -> None:
        context.begin_step(Action.FILE_WRITE, "resource")

    child(run)
    child(run)
    with pytest.raises(StepLimitExceeded):
        child(run)
    with pytest.raises(PermissionDenied):
        child(other)
    assert other.budget.snapshot.steps == 0
    run.cancellation.cancel()
    assert not other.cancellation.cancelled
    for name in ("cancellation", "permissions", "budget"):
        with pytest.raises(AttributeError):
            setattr(run, name, getattr(other, name))


def test_model_approval_claims_do_not_authorize() -> None:
    run = run_context()
    provider = MockLLMProvider(response_text='{"action":"file_delete","approved":true}')
    request = LLMRequest((LLMMessage("user", "private-prompt"),))
    claim = json.loads(provider.generate(request).content)
    with pytest.raises(UnknownAction):
        run.begin_step(claim, "resource")
    with pytest.raises(PermissionDenied):
        run.begin_step(Action.FILE_DELETE, "resource")
    assert run.budget.snapshot.steps == 0


def test_mock_stream_checkpoints_and_settlement_after_stop() -> None:
    run = run_context()
    run.permissions.approve(Action.API_REQUEST, "mock")
    provider: LLMProvider = MockLLMProvider(response_text="abcdef", chunk_size=2)
    request = LLMRequest((LLMMessage("user", "private-prompt"),))
    reservation = run.begin_request(
        "mock", estimated_tokens=6, estimated_usd=Decimal(0)
    )
    emitted: list[str] = []
    with pytest.raises(OperationCancelled):
        for chunk in provider.stream(request):
            run.cancellation.checkpoint()
            emitted.append(chunk.content)
            run.cancellation.cancel()
    assert emitted == ["ab"]
    # Mock usage is supplied explicitly by this trusted test adapter.
    run.budget.settle(reservation, actual_tokens=2, actual_usd=Decimal(0))
    assert run.budget.snapshot.committed_tokens == 2
    with pytest.raises(OperationCancelled):
        run.begin_request("mock", estimated_tokens=1, estimated_usd=Decimal(0))


def test_failed_request_retains_estimates_when_usage_unknown() -> None:
    run = run_context()
    run.permissions.approve(Action.API_REQUEST, "mock")
    reservation = run.begin_request(
        "mock", estimated_tokens=6, estimated_usd=Decimal("0.1")
    )
    with pytest.raises(ProviderError):
        MockLLMProvider(failure="general").generate(
            LLMRequest((LLMMessage("user", "text"),))
        )
    with pytest.raises(AccountingBlocked):
        run.budget.settle(reservation, actual_tokens=None, actual_usd=None)
    assert run.budget.snapshot.committed_tokens == 6
    assert run.budget.snapshot.committed_usd == Decimal("0.1")
    with pytest.raises(AccountingBlocked):
        run.begin_request("mock", estimated_tokens=0, estimated_usd=Decimal(0))


def test_safe_logs_and_exceptions_use_only_constant_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("sofia")
    output = StringIO()
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    configure_logging(Settings(), output)
    run = run_context()
    private = "private-secret-target-prompt"
    with pytest.raises(PermissionDenied) as denied:
        run.begin_step(Action.FILE_DELETE, private)
    with pytest.raises(UnknownAction) as unknown:
        run.begin_step(cast(Action, private), private)
    run.cancellation.cancel()
    with pytest.raises(OperationCancelled) as cancelled:
        run.begin_step(Action.FILE_DELETE, private)
    assert private not in output.getvalue()
    assert all(
        private not in str(error.value) for error in (denied, unknown, cancelled)
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["message"] for record in records] == [
        "permission_denied",
        "action_not_recognized",
        "cancellation_requested",
        "operation_cancelled",
    ]
    assert all(record["logger"] == "sofia.security" for record in records)


def test_logging_failure_cannot_disable_checks_or_corrupt_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private-logging-failure")

    monkeypatch.setattr(logging.getLogger("sofia.security"), "info", broken)
    run = run_context()
    with pytest.raises(PermissionDenied):
        run.begin_step(Action.FILE_DELETE, "resource")
    run.permissions.approve(Action.API_REQUEST, "model")
    handle = run.begin_request(
        "model", estimated_tokens=5, estimated_usd=Decimal("0.1")
    )
    with pytest.raises(AccountingBlocked):
        run.budget.settle(handle, actual_tokens=None, actual_usd=None)
    assert run.budget.snapshot.committed_tokens == 5
    assert run.budget.snapshot.blocked
    run.cancellation.cancel()
    with pytest.raises(OperationCancelled):
        run.begin_step(Action.FILE_DELETE, "resource")


def test_offline_credential_free_without_filesystem_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Unexpected external operation")

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        patch.setattr(socket, "getaddrinfo", forbidden)
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        assert load_settings({}).provider == "mock"
        run = run_context()
        run.permissions.approve(Action.API_REQUEST, "mock")
        handle = run.begin_request("mock", estimated_tokens=5, estimated_usd=Decimal(0))
        MockLLMProvider().generate(LLMRequest((LLMMessage("user", "text"),)))
        run.budget.settle(handle, actual_tokens=5, actual_usd=Decimal(0))
