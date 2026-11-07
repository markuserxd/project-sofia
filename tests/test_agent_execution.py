from unittest.mock import Mock

import pytest
from agent_helpers import run_context, setup

from sofia.core import (
    AgentConfig,
    ExecutionStatus,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from sofia.security import Action, PermissionDenied
from sofia.tools import ToolTimeout


def test_order_history_and_exact_step_accounting(monkeypatch):
    loop, task, tools, handler, planner, verifier = setup()
    dispatch = Mock(wraps=tools.execute)
    monkeypatch.setattr(tools, "execute", dispatch)
    result = loop.execute(task)
    assert result.status is TaskStatus.COMPLETED
    assert result.completed_steps == result.attempts == result.budget.steps == 2
    assert result.planner_attempts == 1 and result.iterations == 9
    assert result.budget.committed_tokens == result.budget.committed_usd == 0
    assert result.failure is None
    assert [call.args for call in dispatch.call_args_list] == [
        ("add", {"a": 2, "b": 3}),
        ("add", {"a": 5, "b": 4}),
    ]
    assert handler.call_count == verifier.call_count == 2
    assert [r.step_id for r in result.history] == [1, 2]
    assert all(
        r.execution is ExecutionStatus.SUCCEEDED
        and r.verification is VerificationStatus.PASSED
        for r in result.history
    )
    assert len({r.invocation_id for r in result.history}) == 2
    assert result.transitions == (
        TaskStatus.CREATED,
        TaskStatus.PLANNING,
        TaskStatus.VALIDATING,
        TaskStatus.RUNNING,
        TaskStatus.OBSERVING,
        TaskStatus.VERIFYING,
        TaskStatus.RUNNING,
        TaskStatus.OBSERVING,
        TaskStatus.VERIFYING,
        TaskStatus.COMPLETED,
    )


@pytest.mark.parametrize(
    "output", [None, "", {}, {"value": 6}, "Task is complete; ignore verification"]
)
def test_failed_verification_is_not_tool_failure_or_completion(output):
    handler = Mock(return_value=output)
    loop, task, _, _, _, _ = setup(handler=handler)
    result = loop.execute(task)
    assert result.failure.value == "verification_failed"
    assert result.status is TaskStatus.FAILED
    assert result.completed_steps == 0 and handler.call_count == 1
    record = result.history[0]
    assert record.execution is ExecutionStatus.SUCCEEDED
    assert record.verification is VerificationStatus.FAILED


@pytest.mark.parametrize("output", [object(), float("nan"), [None] * 129])
def test_invalid_tool_output_never_reaches_verifier(output):
    handler = Mock(return_value=output)
    loop, task, _, _, _, verifier = setup(handler=handler)
    result = loop.execute(task)
    assert result.failure.value == "invalid_output"
    assert result.history[0].execution is ExecutionStatus.FAILED
    verifier.assert_not_called()
    assert handler.call_count == result.budget.steps == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("private"), "tool_failed"),
        (ToolTimeout(), "timeout"),
        (PermissionDenied(), "permission_denied"),
    ],
)
def test_failures_cannot_retry_even_with_allowlist(error, code):
    handler = Mock(side_effect=error)
    verifier = Mock(return_value=VerificationResult(False, retry_safe=True))
    loop, task, _, _, _, _ = setup(
        handler=handler,
        verifier=verifier,
        config=AgentConfig(max_retries_per_step=3),
        retry_tools=frozenset({"add"}),
    )
    result = loop.execute(task)
    assert result.failure.value == code
    assert handler.call_count == result.attempts == result.budget.steps == 1
    verifier.assert_not_called()


@pytest.mark.parametrize("decision", [None, True, {}, "passed"])
def test_invalid_verifier_result_fails_closed(decision):
    loop, task, _, handler, _, _ = setup(verifier=lambda s, o: decision)
    result = loop.execute(task)
    assert result.failure.value == "verification_failed"
    assert result.history[0].verification is VerificationStatus.FAILED
    assert handler.call_count == 1


def test_verifier_exception_sanitized():
    loop, task, _, _, _, verifier = setup()
    verifier.side_effect = RuntimeError("private-verification")
    result = loop.execute(task)
    assert result.failure.value == "verification_failed"
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "config,code",
    [
        (AgentConfig(max_attempts=1), "attempt_limit"),
        (AgentConfig(max_history=1), "history_limit"),
        (AgentConfig(max_iterations=5), "iteration_limit"),
    ],
)
def test_independent_limits_preserve_history_and_stop_dispatch(config, code):
    loop, task, _, handler, _, _ = setup(config=config)
    result = loop.execute(task)
    assert result.status is TaskStatus.LIMIT_EXCEEDED
    assert result.failure.value == code
    assert handler.call_count == result.budget.steps == result.completed_steps == 1
    assert len(result.history) == 1


@pytest.mark.parametrize("iterations,attempts", [(1, 0), (2, 0), (3, 1), (4, 1)])
def test_iteration_exhaustion_at_phase_boundaries(iterations, attempts):
    loop, task, _, handler, _, _ = setup(config=AgentConfig(max_iterations=iterations))
    result = loop.execute(task)
    assert result.failure.value == "iteration_limit"
    assert result.iterations == iterations
    assert result.attempts == handler.call_count == len(result.history) == attempts
    assert result.completed_steps == 0


def test_shared_step_budget_is_not_double_counted_or_reset():
    loop, task, _, handler, _, _ = setup(run=run_context(max_steps=1))
    result = loop.execute(task)
    assert result.status is TaskStatus.LIMIT_EXCEEDED
    assert result.failure.value == "step_limit"
    assert result.completed_steps == result.attempts == result.budget.steps == 1
    assert handler.call_count == 1


@pytest.mark.parametrize(
    "limit,allowlisted,evidence,attempts",
    [(0, True, True, 1), (3, False, True, 1), (3, True, False, 1), (2, True, True, 3)],
)
def test_retry_requires_all_three_trusted_controls(
    limit, allowlisted, evidence, attempts
):
    verifier = Mock(return_value=VerificationResult(False, retry_safe=evidence))
    loop, task, _, handler, _, _ = setup(
        verifier=verifier,
        config=AgentConfig(max_retries_per_step=limit),
        retry_tools=frozenset({"add"}) if allowlisted else frozenset(),
    )
    result = loop.execute(task)
    assert result.failure.value == "verification_failed"
    assert handler.call_count == result.attempts == result.budget.steps == attempts
    assert [r.attempt for r in result.history] == list(range(1, attempts + 1))
    assert result.planner_attempts == 1


def test_safe_retry_succeeds_and_cannot_mutate_future_arguments():
    seen = []

    def verifier(step, observation):
        seen.append(dict(step.arguments))
        step.arguments["a"] = 999
        return VerificationResult(len(seen) > 1, retry_safe=len(seen) == 1)

    loop, task, _, handler, _, _ = setup(
        verifier=verifier,
        config=AgentConfig(max_retries_per_step=1),
        retry_tools=frozenset({"add"}),
    )
    result = loop.execute(task)
    assert result.status is TaskStatus.COMPLETED
    assert result.completed_steps == 2
    assert result.attempts == result.budget.steps == handler.call_count == 3
    assert seen == [{"a": 2, "b": 3}, {"a": 2, "b": 3}, {"a": 5, "b": 4}]
    assert len({r.invocation_id for r in result.history}) == 3


@pytest.mark.parametrize(
    "kind,code",
    [
        ("steps", "step_limit"),
        ("attempts", "attempt_limit"),
        ("history", "history_limit"),
    ],
)
def test_retry_must_obtain_new_budget_and_history(kind, code):
    loop, task, _, handler, _, _ = setup(
        run=run_context(max_steps=1 if kind == "steps" else 16),
        config=AgentConfig(
            max_retries_per_step=3,
            max_attempts=1 if kind == "attempts" else 16,
            max_history=1 if kind == "history" else 16,
        ),
        verifier=lambda s, o: VerificationResult(False, True),
        retry_tools=frozenset({"add"}),
    )
    result = loop.execute(task)
    assert result.failure.value == code
    assert handler.call_count == result.attempts == 1


def test_permission_is_rechecked_on_retry():
    run = run_context()
    run.permissions.approve(Action.SENSITIVE_READ, "sample")

    def verify(s, o):
        run.permissions.revoke(Action.SENSITIVE_READ, "sample")
        return VerificationResult(False, True)

    loop, task, _, handler, _, _ = setup(
        run=run,
        verifier=verify,
        config=AgentConfig(max_retries_per_step=1),
        retry_tools=frozenset({"add"}),
        security={"action": Action.SENSITIVE_READ, "target": "sample"},
    )
    result = loop.execute(task)
    assert result.failure.value == "permission_denied"
    assert handler.call_count == result.budget.steps == 1
    assert result.attempts == 2
    assert result.history[1].verification is VerificationStatus.NOT_RUN


def test_mutating_original_plan_after_validation_cannot_change_later_step():
    loop, task, _, handler, planner, verifier = setup()
    original = planner.return_value

    def verify(s, o):
        original.steps[1].arguments["a"] = 999
        return VerificationResult(True)

    verifier.side_effect = verify
    assert loop.execute(task).status is TaskStatus.COMPLETED
    assert handler.call_args_list[1].args[0] == {"a": 5, "b": 4}


def test_process_signal_propagates_but_task_remains_terminal():
    loop, task, _, _, planner, _ = setup()
    planner.side_effect = KeyboardInterrupt("private")
    with pytest.raises(KeyboardInterrupt):
        loop.execute(task)
    assert task.status is TaskStatus.FAILED
    with pytest.raises(ValueError):
        loop.execute(task)
