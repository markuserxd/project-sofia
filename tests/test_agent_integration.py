import builtins
import json
import runpy
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
from agent_helpers import setup
from anthropic_helpers import payload

from sofia.core import AgentLoop, AgentPlan, LLMPlanner, TaskStatus, VerificationResult
from sofia.providers import MockLLMProvider
from sofia.providers.routing import ModelRouter

PLAN = '{"steps":[{"tool":"add","arguments":{"a":2,"b":3}},{"tool":"add","arguments":{"a":5,"b":4}}]}'


@pytest.mark.parametrize(
    "mode,status,completed",
    [
        ("success", TaskStatus.COMPLETED, 2),
        ("failure", TaskStatus.FAILED, 1),
        ("cancel", TaskStatus.CANCELLED, 1),
    ],
)
def test_offline_demo_has_no_file_or_network_effects(
    mode, status, completed, monkeypatch
):
    demo = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "agent_loop.py"))

    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected filesystem effect")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        loop, task = demo["build_demo"](mode)
        result = loop.execute(task)
    assert result.status is status
    assert result.completed_steps == completed
    assert result.budget.steps == result.attempts == 2
    assert result.budget.committed_tokens == result.budget.committed_usd == 0
    assert [r.tool for r in result.history] == ["add", "multiply"]


@pytest.mark.parametrize("partial", [True, False])
def test_successful_steps_do_not_prove_task_complete(partial):
    demo = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "agent_loop.py"))
    loop, task = demo["build_demo"]()
    plan = loop._planner(task.instruction)
    steps = plan.steps[:1] if partial else tuple(reversed(plan.steps))
    loop._planner = lambda instruction: AgentPlan(steps)
    result = loop.execute(task)
    assert result.status is TaskStatus.FAILED
    assert result.failure.value == "verification_failed"
    assert result.completed_steps == len(steps)


@pytest.mark.parametrize("routed", [False, True])
def test_paid_planning_uses_existing_adapter_accounting_offline(claude, routed):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(200, json=payload(PLAN))
    )
    loop, task, tools, handler, _, verifier = setup(run=run)
    selected = (
        ModelRouter(run=run, routes={"paid": provider}).select("paid")
        if routed
        else provider
    )
    loop = AgentLoop(
        registry=tools,
        planner=LLMPlanner(selected, run),
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(len(records) == 2),
    )
    result = loop.execute(task)
    assert result.status is TaskStatus.COMPLETED
    assert result.attempts == handler.call_count == 2
    assert result.budget.steps == 3  # Exactly one paid plan plus two tools.
    assert result.budget.committed_tokens == 27
    assert result.budget.committed_usd > 0
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert len(sent["messages"]) == 1
    assert sent["messages"][0]["content"] == task.instruction


@pytest.mark.parametrize(
    "condition,code,dispatches",
    [
        ("permission", "permission_denied", 0),
        ("tokens", "token_limit", 0),
        ("usd", "spending_limit", 0),
        ("steps", "step_limit", 0),
        ("accounting", "accounting_blocked", 1),
        ("timeout", "accounting_blocked", 1),
        ("malformed", "malformed_plan", 1),
    ],
)
def test_paid_planner_failures_never_retry_or_dispatch_tools(
    claude, condition, code, dispatches
):
    def respond(req, run):
        if condition == "timeout":
            raise httpx2.ReadTimeout("private-network-error", request=req)
        return httpx2.Response(
            200,
            json=payload(
                "not JSON" if condition == "malformed" else PLAN,
                **({"usage": None} if condition == "accounting" else {}),
            ),
        )

    limits = {
        "tokens": {"max_tokens": 0},
        "usd": {"max_usd": Decimal(0)},
        "steps": {"max_steps": 0},
    }.get(condition, {})
    provider, run, calls = claude(respond, approve=condition != "permission", **limits)
    _, task, tools, handler, _, verifier = setup(run=run)
    loop = AgentLoop(
        registry=tools,
        planner=LLMPlanner(provider, run),
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(True),
    )
    result = loop.execute(task)
    assert result.failure.value == code
    assert len(calls) == dispatches
    assert result.planner_attempts <= 1
    assert result.attempts == 0
    handler.assert_not_called()


def test_planning_cannot_reset_paid_step_budget(claude):
    provider, run, calls = claude(
        lambda req, run: httpx2.Response(200, json=payload(PLAN)), max_steps=2
    )
    _, task, tools, handler, _, verifier = setup(run=run)
    loop = AgentLoop(
        registry=tools,
        planner=LLMPlanner(provider, run),
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(True),
    )
    result = loop.execute(task)
    assert result.failure.value == "step_limit"
    assert result.completed_steps == handler.call_count == 1
    assert result.budget.steps == 2 and len(calls) == 1


def test_provider_failure_is_fixed_and_never_retried():
    _, task, tools, handler, _, verifier = setup()
    planner = LLMPlanner(MockLLMProvider(failure="temporary"), task.run)
    loop = AgentLoop(
        registry=tools,
        planner=planner,
        verifier=verifier,
        completion_verifier=lambda records: VerificationResult(True),
    )
    result = loop.execute(task)
    assert result.failure.value == "provider_failed"
    assert result.planner_attempts == 1
    handler.assert_not_called()


@pytest.mark.parametrize("condition", ["cancel", "false", "exception", "invalid"])
def test_final_verification_cannot_be_skipped_or_override_cancellation(condition):
    loop, task, _, _, _, _ = setup()

    def complete(records):
        assert len(records) == 2
        if condition == "cancel":
            task.run.cancellation.cancel()
            return VerificationResult(True)
        if condition == "exception":
            raise RuntimeError("private")
        return None if condition == "invalid" else VerificationResult(False)

    loop._completion_verifier = complete
    result = loop.execute(task)
    assert result.status is (
        TaskStatus.CANCELLED if condition == "cancel" else TaskStatus.FAILED
    )
    assert result.completed_steps == 2


def test_final_verification_counts_against_orchestration_limit():
    from sofia.core import AgentConfig

    complete = Mock(return_value=VerificationResult(True))
    loop, task, _, _, _, _ = setup(
        config=AgentConfig(max_iterations=8), completion_verifier=complete
    )
    result = loop.execute(task)
    assert result.failure.value == "iteration_limit"
    assert result.completed_steps == 2
    complete.assert_not_called()
