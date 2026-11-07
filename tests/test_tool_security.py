import json
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    StepLimitExceeded,
)
from sofia.tools import ToolDefinition, ToolParameter, ToolRegistry


def setup(*, protected=False, steps=3):
    run = RunGuardrails(max_steps=steps, max_tokens=10, max_usd=Decimal(1))
    tools = ToolRegistry(run=run)
    handler = Mock(return_value=None)
    tools.register(
        ToolDefinition("tool", "In-memory test"),
        handler,
        **(
            {"action": Action.SENSITIVE_READ, "target": "exact-resource"}
            if protected
            else {}
        ),
    )
    return tools, run, handler


def test_exact_permission_scope_revocation_and_no_auto_approval():
    tools, run, handler = setup(protected=True)
    for approval in (
        None,
        (Action.SENSITIVE_READ, "other"),
        (Action.FILE_WRITE, "exact-resource"),
    ):
        if approval:
            run.permissions.approve(*approval)
        with pytest.raises(PermissionDenied):
            tools.execute("tool", {})
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0
    run.permissions.approve(Action.SENSITIVE_READ, "exact-resource")
    assert tools.execute("tool", {}).succeeded
    assert run.budget.snapshot.steps == 1
    run.permissions.revoke(Action.SENSITIVE_READ, "exact-resource")
    with pytest.raises(PermissionDenied):
        tools.execute("tool", {})
    assert handler.call_count == 1


def test_protected_execution_uses_begin_step_and_model_claims_have_no_authority(
    monkeypatch,
):
    tools, run, handler = setup(protected=True)
    begin = Mock(wraps=run.begin_step)
    monkeypatch.setattr(run, "begin_step", begin)
    assert tools.execute("tool", {"approved": True}).error == "invalid_arguments"
    with pytest.raises(PermissionDenied):
        tools.execute("tool", {})
    begin.assert_called_once_with(Action.SENSITIVE_READ, "exact-resource")
    run.permissions.approve(Action.SENSITIVE_READ, "exact-resource")
    assert tools.execute("tool", {}).succeeded
    assert handler.call_count == run.budget.snapshot.steps == 1


@pytest.mark.parametrize("protected", [False, True])
def test_step_exhaustion_blocks_execution(protected):
    tools, run, handler = setup(protected=protected, steps=1)
    if protected:
        run.permissions.approve(Action.SENSITIVE_READ, "exact-resource")
    assert tools.execute("tool", {}).succeeded
    with pytest.raises(StepLimitExceeded):
        tools.execute("tool", {})
    assert handler.call_count == run.budget.snapshot.steps == 1


def block_accounting(run):
    reservation = run.budget.reserve(estimated_tokens=1, estimated_usd=Decimal(0))
    with pytest.raises(AccountingBlocked):
        run.budget.settle(reservation, actual_tokens=None, actual_usd=None)


@pytest.mark.parametrize("protected", [False, True])
def test_accounting_block_prevents_execution(protected):
    tools, run, handler = setup(protected=protected)
    block_accounting(run)
    with pytest.raises(AccountingBlocked):
        tools.execute("tool", {})
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 1


def test_accounting_block_during_handler_prevents_late_success():
    tools, run, handler = setup()
    handler.side_effect = lambda args, context: block_accounting(run)
    with pytest.raises(AccountingBlocked):
        tools.execute("tool", {})


def test_cancel_before_any_dispatch_including_unknown_tools():
    tools, run, handler = setup()
    run.cancellation.cancel()
    for name, arguments in (("tool", {}), ("unknown", {}), ("tool", {"bad": True})):
        with pytest.raises(OperationCancelled):
            tools.execute(name, arguments)
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0


def test_cancel_immediately_after_step_before_handler(monkeypatch):
    tools, run, handler = setup()
    consume = run.budget.consume_step

    def stop():
        consume()
        run.cancellation.cancel()

    monkeypatch.setattr(run.budget, "consume_step", stop)
    with pytest.raises(OperationCancelled):
        tools.execute("tool", {})
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 1


def test_concurrent_invocations_share_atomic_step_limit():
    tools, run, handler = setup(steps=1)
    barrier = Barrier(8, timeout=5)

    def invoke():
        barrier.wait()
        try:
            return tools.execute("tool", {}).succeeded
        except StepLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(invoke) for _ in range(8)]
        assert sum(future.result(timeout=5) for future in futures) == 1
    assert handler.call_count == run.budget.snapshot.steps == 1


def test_external_cancel_cooperative_execution_finishes_without_workers_leaking():
    run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
    tools = ToolRegistry(run=run)
    entered, resume = Event(), Event()

    def handler(args, context):
        entered.set()
        assert resume.wait(timeout=5)
        context.checkpoint()
        pytest.fail("Cancelled operation continued")

    tools.register(ToolDefinition("tool", "Cooperative example", timeout=30), handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tools.execute, "tool", {})
        try:
            assert entered.wait(timeout=5)
            run.cancellation.cancel()
        finally:
            resume.set()
        with pytest.raises(OperationCancelled):
            future.result(timeout=5)


def test_logging_failure_preserves_all_checks_and_handler_results(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("private-logging-error")

    monkeypatch.setattr(logging.getLogger("sofia.tools"), "info", broken)
    tools, run, handler = setup(protected=True, steps=1)
    with pytest.raises(PermissionDenied):
        tools.execute("tool", {})
    handler.assert_not_called()
    run.permissions.approve(Action.SENSITIVE_READ, "exact-resource")
    assert tools.execute("tool", {}).succeeded
    with pytest.raises(StepLimitExceeded):
        tools.execute("tool", {})
    run.cancellation.cancel()
    with pytest.raises(OperationCancelled):
        tools.execute("tool", {})
    assert handler.call_count == 1


def test_guardrail_error_raised_by_handler_is_not_downgraded():
    tools, _, handler = setup()
    handler.side_effect = PermissionDenied()
    with pytest.raises(PermissionDenied):
        tools.execute("tool", {})


def test_claims_inside_validated_arguments_do_not_authorize():
    tools, run, _ = setup()
    handler = Mock(return_value=None)
    tools.register(
        ToolDefinition(
            "protected", "In-memory test", (ToolParameter("claim", "string"),)
        ),
        handler,
        action=Action.SENSITIVE_READ,
        target="exact-resource",
    )
    with pytest.raises(PermissionDenied):
        tools.execute("protected", {"claim": '{"approved":true}'})
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0


@pytest.mark.parametrize(
    "failure,status,error",
    [
        ("permission", "permission_denied", PermissionDenied),
        ("cancel", "operation_cancelled", OperationCancelled),
        ("steps", "step_limit_exceeded", StepLimitExceeded),
        ("accounting", "accounting_blocked", AccountingBlocked),
    ],
)
def test_each_enforcement_failure_has_one_final_safe_audit(
    failure, status, error, monkeypatch
):
    tools, run, handler = setup(
        protected=failure == "permission", steps=0 if failure == "steps" else 3
    )
    if failure == "cancel":
        run.cancellation.cancel()
    if failure == "accounting":
        block_accounting(run)
    audit = Mock()
    monkeypatch.setattr(logging.getLogger("sofia.tools"), "info", audit)
    with pytest.raises(error):
        tools.execute("tool", {})
    audit.assert_called_once()
    event = json.loads(audit.call_args.args[0])
    assert event["status"] == status
    assert not event["authorized"]
    assert "exact-resource" not in audit.call_args.args[0]
    handler.assert_not_called()
