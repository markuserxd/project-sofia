"""Pure in-memory agent fixtures; no provider clients or side effects."""

from decimal import Decimal
from unittest.mock import Mock

from sofia.core import (
    AgentConfig,
    AgentLoop,
    AgentPlan,
    AgentTask,
    PlannedStep,
    VerificationResult,
)
from sofia.security import RunGuardrails
from sofia.tools import ToolDefinition, ToolParameter, ToolRegistry


def run_context(**changes):
    return RunGuardrails(
        **({"max_steps": 16, "max_tokens": 0, "max_usd": Decimal(0)} | changes)
    )


def setup(
    *,
    handler=None,
    verifier=None,
    completion_verifier=None,
    planner=None,
    run=None,
    config=None,
    retry_tools=frozenset(),
    security=None,
):
    run = run if run is not None else run_context()
    tools = ToolRegistry(run=run)
    handler = (
        handler
        if handler is not None
        else Mock(side_effect=lambda a, c: {"value": a["a"] + a["b"]})
    )
    tools.register(
        ToolDefinition(
            "add",
            "Add in memory",
            (ToolParameter("a", "integer"), ToolParameter("b", "integer")),
        ),
        handler,
        **(security or {}),
    )
    plan = AgentPlan(
        (PlannedStep("add", {"a": 2, "b": 3}), PlannedStep("add", {"a": 5, "b": 4}))
    )
    planner = planner if planner is not None else Mock(return_value=plan)
    verifier = (
        verifier
        if verifier is not None
        else Mock(
            side_effect=lambda s, o: VerificationResult(
                o.output == {"value": s.arguments["a"] + s.arguments["b"]}
            )
        )
    )
    loop = AgentLoop(
        registry=tools,
        planner=planner,
        verifier=verifier,
        retry_tools=retry_tools,
        completion_verifier=completion_verifier
        or (lambda records: VerificationResult(bool(records))),
    )
    task = AgentTask("private task instruction", run, config or AgentConfig())
    return loop, task, tools, handler, planner, verifier
