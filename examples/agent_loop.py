"""Opt-in offline demonstration: python examples/agent_loop.py."""

from collections.abc import Mapping
from decimal import Decimal

from sofia.core import (
    AgentConfig,
    AgentLoop,
    AgentPlan,
    AgentTask,
    Observation,
    PlannedStep,
    StepRecord,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from sofia.security import RunGuardrails
from sofia.tools import (
    JSONValue,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)


def build_demo(mode: str = "success") -> tuple[AgentLoop, AgentTask]:
    if mode not in ("success", "failure", "cancel"):
        raise ValueError("invalid_demo_mode")
    run = RunGuardrails(max_steps=2, max_tokens=0, max_usd=Decimal(0))
    registry = ToolRegistry(run=run)

    def add(arguments: Mapping[str, JSONValue], context: ToolContext) -> JSONValue:
        context.checkpoint()
        a, b = arguments["a"], arguments["b"]
        assert type(a) is int and type(b) is int
        return {"value": a + b}

    def multiply(arguments: Mapping[str, JSONValue], context: ToolContext) -> JSONValue:
        if mode == "cancel":
            run.cancellation.cancel()
        context.checkpoint()
        if mode == "failure":
            raise RuntimeError("simulated_failure")
        a, b = arguments["a"], arguments["b"]
        assert type(a) is int and type(b) is int
        return {"value": a * b}

    parameters = (ToolParameter("a", "integer"), ToolParameter("b", "integer"))
    registry.register(ToolDefinition("add", "Add integers in memory", parameters), add)
    registry.register(
        ToolDefinition("multiply", "Multiply integers in memory", parameters), multiply
    )

    def plan(instruction: str) -> AgentPlan:
        return AgentPlan(
            (
                PlannedStep("add", {"a": 2, "b": 3}),
                PlannedStep("multiply", {"a": 5, "b": 4}),
            )
        )

    def verify(step: PlannedStep, observation: Observation) -> VerificationResult:
        expected = {"add": ({"a": 2, "b": 3}, 5), "multiply": ({"a": 5, "b": 4}, 20)}
        if step.tool not in expected:
            return VerificationResult(False)
        arguments, value = expected[step.tool]
        output = observation.output
        return VerificationResult(
            step.arguments == arguments
            and type(output) is dict
            and output.keys() == {"value"}
            and type(output["value"]) is int
            and output["value"] == value
        )

    def complete(records: tuple[StepRecord, ...]) -> VerificationResult:
        # Step checks establish exact inputs/outputs; this proves the whole task
        # sequence, rejecting a partial, reordered, or repeated model plan.
        return VerificationResult(
            tuple(r.tool for r in records) == ("add", "multiply")
            and all(r.verification is VerificationStatus.PASSED for r in records)
        )

    loop = AgentLoop(
        registry=registry, planner=plan, verifier=verify, completion_verifier=complete
    )
    task = AgentTask(
        "Add 2 and 3, then multiply 5 by 4; verify 20.",
        run,
        AgentConfig(max_plan_steps=2, max_attempts=2, max_history=2),
    )
    return loop, task


def main() -> None:
    for mode, expected in (
        ("success", TaskStatus.COMPLETED),
        ("failure", TaskStatus.FAILED),
        ("cancel", TaskStatus.CANCELLED),
    ):
        loop, task = build_demo(mode)
        result = loop.execute(task)
        assert result.status is expected
        print(
            f"{mode}: {result.status.value}; verified={result.completed_steps}; steps={result.budget.steps}"
        )


if __name__ == "__main__":
    main()
