import json
import logging
from dataclasses import asdict
from decimal import Decimal
from io import StringIO
from threading import get_ident
from unittest.mock import Mock

import pytest

from sofia.config import Settings
from sofia.logging_setup import configure_logging, task_log_context
from sofia.security import OperationCancelled, RunGuardrails
from sofia.tools import ToolDefinition, ToolParameter, ToolRegistry, ToolTimeout


class Clock:
    now = 10.0

    def __call__(self):
        return self.now


@pytest.fixture
def audit(monkeypatch):
    logger = logging.getLogger("sofia")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    output = StringIO()
    configure_logging(Settings(), output)
    return output


def audit_events(output):
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    return [json.loads(r["message"]) for r in records if r["logger"] == "sofia.tools"]


def setup(handler, *, clock=None, parameters=()):
    run = RunGuardrails(max_steps=10, max_tokens=10, max_usd=Decimal(1))
    tools = ToolRegistry(run=run, **({"clock": clock} if clock else {}))
    tools.register(
        ToolDefinition("tool", "Safe example", parameters, timeout=2), handler
    )
    return tools, run


def test_structured_success_null_output_unique_invocations(audit):
    tools, run = setup(lambda args, context: None)
    with task_log_context("opaque-task"):
        first = tools.execute("tool", {})
        second = tools.execute("tool", {})
    assert first.succeeded and first.error is None and first.output is None
    assert first.tool == "tool"
    assert first.invocation_id != second.invocation_id
    assert run.budget.snapshot.steps == 2
    assert json.loads(json.dumps(asdict(first)))["error"] is None
    events = audit_events(audit)
    assert [e["status"] for e in events] == ["success", "success"]
    assert [e["invocation_id"] for e in events] == [
        first.invocation_id,
        second.invocation_id,
    ]
    assert all(e["authorized"] for e in events)
    assert all(
        json.loads(line)["task_id"] == "opaque-task"
        for line in audit.getvalue().splitlines()
    )


@pytest.mark.parametrize(
    "error", [ValueError("private-exception"), RuntimeError("private-exception")]
)
def test_handler_failures_are_sanitized_and_spend_step(error, audit):
    def fail(args, context):
        raise error

    tools, run = setup(fail)
    result = tools.execute("tool", {})
    assert not result.succeeded and result.error == "handler_failed"
    assert result.output is None
    assert "private-exception" not in repr(result) + audit.getvalue()
    assert run.budget.snapshot.steps == 1
    assert audit_events(audit)[0]["status"] == "handler_failed"


@pytest.mark.parametrize(
    "output", [object(), {"secret": object()}, float("nan"), [None] * 129]
)
def test_invalid_outputs(output, audit):
    tools, _ = setup(lambda args, context: output)
    result = tools.execute("tool", {})
    assert result.error == "invalid_output" and result.output is None
    assert audit_events(audit)[0]["status"] == "invalid_output"


@pytest.mark.parametrize("cooperative", [True, False])
def test_monotonic_deadline_and_synchronous_execution(cooperative, audit):
    clock = Clock()
    thread = get_ident()

    def handler(args, context):
        assert get_ident() == thread
        assert context.deadline == 12
        clock.now = 12
        if cooperative:
            context.checkpoint()
        return "late-success"

    tools, run = setup(handler, clock=clock)
    result = tools.execute("tool", {})
    assert result.error == "timeout" and result.output is None
    assert run.budget.snapshot.steps == 1
    assert audit_events(audit)[0]["status"] == "timeout"


def test_expired_deadline_before_invocation(audit):
    times = iter([10, 12])
    handler = Mock()
    tools, run = setup(handler, clock=lambda: next(times))
    assert tools.execute("tool", {}).error == "timeout"
    handler.assert_not_called()
    assert run.budget.snapshot.steps == 0


@pytest.mark.parametrize("check_inside", [True, False])
def test_cancel_during_handler_or_before_acceptance(check_inside, audit):
    def handler(args, context):
        run.cancellation.cancel()
        if check_inside:
            context.checkpoint()
        return "late-success"

    tools, run = setup(handler)
    with pytest.raises(OperationCancelled):
        tools.execute("tool", {})
    assert audit_events(audit)[0]["status"] == "operation_cancelled"


def test_process_signals_propagate_and_are_audited(audit):
    def handler(args, context):
        raise KeyboardInterrupt("private-interrupt")

    tools, _ = setup(handler)
    with pytest.raises(KeyboardInterrupt):
        tools.execute("tool", {})
    assert audit_events(audit)[0]["status"] == "interrupted"
    assert "private-interrupt" not in audit.getvalue()


def test_audit_never_includes_arguments_outputs_names_or_exception_text(audit):
    private = (
        "C:/private/folder https://private.example/account unguessable-sensitive-value"
    )
    tools, _ = setup(
        lambda args, context: {"value": private},
        parameters=(ToolParameter("text", "string"),),
    )
    result = tools.execute("tool", {"text": private})
    assert result.output == {"value": private}
    assert private not in repr(result)
    assert tools.execute(private, {}).error == "unknown_tool"
    assert tools.execute([], {}).error == "unknown_tool"
    assert tools.execute("tool", {"unknown": private}).error == "invalid_arguments"
    events = audit_events(audit)
    assert [e["status"] for e in events] == [
        "success",
        "unknown_tool",
        "unknown_tool",
        "invalid_arguments",
    ]
    assert events[1]["tool"] is events[2]["tool"] is None
    assert private not in audit.getvalue()
    assert all(
        set(e) == {"event", "invocation_id", "tool", "status", "authorized"}
        for e in events
    )


def test_handler_timeout_is_fixed_failure(audit):
    def handler(args, context):
        raise ToolTimeout()

    tools, _ = setup(handler)
    assert tools.execute("tool", {}).error == "timeout"
