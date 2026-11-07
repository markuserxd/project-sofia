from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event

import pytest
from agent_helpers import run_context, setup

from sofia.core import AgentConfig, AgentTask, TaskState, TaskStatus, VerificationResult


def test_explicit_transitions_and_invalid_jump():
    state = TaskState()
    assert state.status is TaskStatus.CREATED
    with pytest.raises(ValueError, match="invalid_agent_transition"):
        state.transition(TaskStatus.COMPLETED)
    for target in (
        TaskStatus.PLANNING,
        TaskStatus.VALIDATING,
        TaskStatus.RUNNING,
        TaskStatus.OBSERVING,
        TaskStatus.VERIFYING,
        TaskStatus.RUNNING,
        TaskStatus.OBSERVING,
        TaskStatus.VERIFYING,
        TaskStatus.COMPLETED,
    ):
        state.transition(target)
        assert state.status is target
    with pytest.raises(AttributeError):
        state.status = TaskStatus.CREATED


@pytest.mark.parametrize("terminal", [s for s in TaskStatus if s.terminal])
def test_all_terminal_states_are_irreversible(terminal):
    state = TaskState()
    for target in (
        TaskStatus.PLANNING,
        TaskStatus.VALIDATING,
        TaskStatus.RUNNING,
        TaskStatus.OBSERVING,
        TaskStatus.VERIFYING,
        terminal,
    ):
        state.transition(target)
    for target in TaskStatus:
        with pytest.raises(ValueError):
            state.transition(target)


@pytest.mark.parametrize("terminal", ["success", "failure", "cancel", "limit"])
def test_task_cannot_execute_again(terminal):
    loop, task, _, handler, _, _ = setup(
        config=AgentConfig(max_attempts=1) if terminal == "limit" else None,
        verifier=(lambda s, o: VerificationResult(False))
        if terminal == "failure"
        else None,
    )
    if terminal == "cancel":
        task.run.cancellation.cancel()
    result = loop.execute(task)
    assert result.status.terminal
    count = handler.call_count
    with pytest.raises(ValueError, match="task_already_executed"):
        loop.execute(task)
    assert handler.call_count == count
    with pytest.raises(FrozenInstanceError):
        result.status = TaskStatus.CREATED


def test_concurrent_and_recursive_execution_rejected():
    entered, release = Event(), Event()

    def planner(instruction):
        entered.set()
        assert release.wait(5)
        with pytest.raises(ValueError, match="task_already_executed"):
            loop.execute(task)
        return original.return_value

    loop, task, _, _, original, _ = setup()
    loop._planner = planner
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loop.execute, task)
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="task_already_executed"):
                loop.execute(task)
        finally:
            release.set()
        assert future.result(5).status is TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "field",
    [
        "max_plan_steps",
        "max_attempts",
        "max_retries_per_step",
        "max_iterations",
        "max_history",
    ],
)
@pytest.mark.parametrize("value", [-1, True, False, 1.0, "2", None, 10**10])
def test_invalid_limits(field, value):
    with pytest.raises(ValueError, match="invalid_agent_config"):
        AgentConfig(**{field: value})


@pytest.mark.parametrize(
    "field", ["max_plan_steps", "max_attempts", "max_iterations", "max_history"]
)
def test_zero_positive_limits(field):
    with pytest.raises(ValueError):
        AgentConfig(**{field: 0})


@pytest.mark.parametrize("instruction", [None, True, "", " ", "x" * 8193, object()])
def test_invalid_task_rejected_before_execution(instruction):
    with pytest.raises(ValueError, match="^invalid_task$"):
        AgentTask(instruction, run_context())


@pytest.mark.parametrize("value", [None, "created", [], TaskStatus.CREATED])
def test_invalid_state_target(value):
    with pytest.raises(ValueError):
        TaskState().transition(value)


@pytest.mark.parametrize(
    "passed,retry", [(1, False), (False, 1), (True, True), (None, False)]
)
def test_invalid_verification_configuration(passed, retry):
    with pytest.raises(ValueError, match="invalid_verification"):
        VerificationResult(passed, retry)
