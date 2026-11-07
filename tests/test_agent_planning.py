import json
from unittest.mock import Mock

import pytest
from agent_helpers import setup

from sofia.core import (
    AgentConfig,
    AgentLoop,
    AgentPlan,
    LLMPlanner,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.core.planning import PlanError, parse_plan, validate_plan
from sofia.providers import MockLLMProvider
from sofia.tools import ToolDefinition, ToolParameter

VALID = '{"steps":[{"tool":"add","arguments":{"a":2,"b":3}}]}'


@pytest.mark.parametrize(
    "text",
    [
        "Hello from SOFIA",
        "",
        "null",
        "[]",
        "{}",
        '{"steps":[]}',
        '{"steps":{},"approved":true}',
        '{"steps":[{}]}',
        '{"steps":[{"tool":"add"}]}',
        '{"steps":[{"tool":"add","arguments":[]}]}',
        VALID[:-1],
        "```json\n" + VALID + "\n```",
        VALID + " trailing",
        VALID.replace('"a":2', '"a":2,"a":3'),
        VALID.replace('"a":2', '"a":NaN'),
        VALID.replace('"a":2', '"a":Infinity'),
        VALID.replace('"a":2', '"a":-Infinity'),
        '{"steps":[],"steps":[]}',
        "[" * 2000 + "]" * 2000,
        " " * 65537,
        json.dumps({"steps": [{"tool": "add", "arguments": {}}] * 129}),
    ],
)
def test_strict_json_rejected_without_dispatch(text):
    loop, task, tools, handler, _, verifier = setup()
    loop = AgentLoop(
        registry=tools,
        planner=LLMPlanner(MockLLMProvider(response_text=text), task.run),
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(len(records) == 1),
    )
    result = loop.execute(task)
    assert result.status is TaskStatus.FAILED
    assert result.failure.value == "malformed_plan"
    assert result.attempts == 0 and result.planner_attempts == 1
    handler.assert_not_called()
    assert result.budget.steps == 0


@pytest.mark.parametrize(
    "key",
    [
        "approved",
        "permissions",
        "budget",
        "timeout",
        "provider",
        "handler",
        "python",
        "shell",
        "verify",
        "retry_safe",
    ],
)
def test_security_and_executable_fields_never_accepted(key):
    raw = json.loads(VALID)
    for location in (raw, raw["steps"][0]):
        location[key] = "__import__('os').system('never execute')"
        with pytest.raises(PlanError, match="malformed_plan"):
            parse_plan(json.dumps(raw))
        del location[key]


@pytest.mark.parametrize(
    "bad,code",
    [
        (PlannedStep("unknown", {}), "unknown_tool"),
        (PlannedStep("../private", {}), "unknown_tool"),
        (PlannedStep([], {}), "unknown_tool"),
        (PlannedStep("add", {}), "invalid_arguments"),
        (PlannedStep("add", {"a": True, "b": 3}), "invalid_arguments"),
        (PlannedStep("add", {"a": 2, "b": 3, "approved": True}), "invalid_arguments"),
        (PlannedStep("add", {"a": object(), "b": 3}), "invalid_arguments"),
        (object(), "malformed_plan"),
    ],
)
def test_whole_plan_validated_before_first_tool(bad, code):
    loop, task, _, handler, planner, _ = setup()
    planner.return_value = AgentPlan((planner.return_value.steps[0], bad))
    result = loop.execute(task)
    assert result.failure.value == code
    assert result.history == () and result.budget.steps == 0
    handler.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        AgentPlan(()),
        AgentPlan([]),
        AgentPlan((PlannedStep("add", {}),) * 129),
    ],
)
def test_invalid_typed_plan(bad):
    loop, task, _, handler, planner, _ = setup()
    planner.return_value = bad
    assert loop.execute(task).failure.value == "malformed_plan"
    handler.assert_not_called()


def test_lower_step_limit_and_planner_exception():
    loop, task, _, handler, _, _ = setup(config=AgentConfig(max_plan_steps=1))
    assert loop.execute(task).failure.value == "malformed_plan"
    handler.assert_not_called()
    loop, task, _, _, planner, _ = setup()
    planner.side_effect = RuntimeError("private exception")
    assert loop.execute(task).failure.value == "planning_failed"
    assert planner.call_count == 1


def test_typed_and_json_plans_share_bounded_argument_validator():
    loop, task, tools, handler, planner, _ = setup()
    tools.register(
        ToolDefinition("echo", "Test", (ToolParameter("value", "array"),)), handler
    )
    nested = []
    for _ in range(9):
        nested = [nested]
    cyclic = []
    cyclic.append(cyclic)
    for value in (
        nested,
        cyclic,
        [None] * 129,
        ["x" * 8193],
        [[None] * 128 for _ in range(40)],
    ):
        with pytest.raises(PlanError, match="invalid_arguments"):
            validate_plan(
                AgentPlan((PlannedStep("echo", {"value": value}),)), tools, 16
            )
    big = AgentPlan(
        tuple(PlannedStep("echo", {"value": ["x" * 8192]}) for _ in range(9))
    )
    with pytest.raises(PlanError, match="malformed_plan"):
        validate_plan(big, tools, 16)
    assert task.run.budget.snapshot.steps == 0


def test_validation_copies_all_arguments():
    _, _, tools, _, planner, _ = setup()
    original = planner.return_value
    validated = validate_plan(original, tools, 16)
    original.steps[0].arguments["a"] = 100
    assert validated.steps[0].arguments == {"a": 2, "b": 3}


def test_deliberate_mock_json_and_ordinary_mock_unchanged():
    _, task, tools, handler, _, verifier = setup()
    provider = MockLLMProvider(response_text=VALID)
    loop = AgentLoop(
        registry=tools,
        planner=LLMPlanner(provider, task.run),
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(len(records) == 1),
    )
    assert loop.execute(task).status is TaskStatus.COMPLETED
    assert handler.call_count == 1
    assert MockLLMProvider().response_text == "Hello from SOFIA"


def test_plan_and_steps_hide_untrusted_repr():
    step = PlannedStep("private untrusted name", {"password": "private value"})
    assert "private" not in repr(step) + repr(AgentPlan((step,)))


def test_foreign_provider_requires_actual_run_identity():
    _, task, _, _, _, _ = setup()
    with pytest.raises(ValueError, match="agent_run_mismatch"):
        LLMPlanner(Mock(spec=["generate", "stream"]), task.run)
