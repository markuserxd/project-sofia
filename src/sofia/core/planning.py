"""Bounded plans are data; only registered tools and their argument schemas apply."""

import json
from dataclasses import dataclass, field
from typing import Protocol

from sofia.providers.base import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from sofia.providers.mock import MockLLMProvider
from sofia.providers.routing import GuardedProvider
from sofia.security import AccountingBlocked, RunGuardrails, StepLimitExceeded
from sofia.tools import JSONValue, ToolRegistry, UnknownTool
from sofia.tools.schemas import InvalidArguments, validate_arguments

MAX_PLAN_TEXT = 65536
MAX_INSTRUCTION = 8192
MAX_PLAN_STEPS = 128


class PlanError(ValueError):
    """Only internal, fixed reason codes are emitted by the parser/validator."""


@dataclass(frozen=True, slots=True, repr=False)
class PlannedStep:
    tool: str
    arguments: dict[str, JSONValue] = field(repr=False)


@dataclass(frozen=True, slots=True)
class AgentPlan:
    steps: tuple[PlannedStep, ...] = field(repr=False)


class Planner(Protocol):
    def __call__(self, instruction: str, /) -> AgentPlan: ...


def _object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise PlanError("malformed_plan")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise PlanError("malformed_plan")


def parse_plan(content: str) -> AgentPlan:
    """Exact JSON only: no fences, duplicate keys, constants, or extra fields."""
    if type(content) is not str or len(content) > MAX_PLAN_TEXT:
        raise PlanError("malformed_plan")
    try:
        raw = json.loads(content, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise PlanError("malformed_plan") from None
    if type(raw) is not dict or raw.keys() != {"steps"}:
        raise PlanError("malformed_plan")
    steps = raw["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= MAX_PLAN_STEPS:
        raise PlanError("malformed_plan")
    result = []
    for step in steps:
        if type(step) is not dict or step.keys() != {"tool", "arguments"}:
            raise PlanError("malformed_plan")
        if type(step["tool"]) is not str or type(step["arguments"]) is not dict:
            raise PlanError("malformed_plan")
        result.append(PlannedStep(step["tool"], step["arguments"]))
    return AgentPlan(tuple(result))


def validate_plan(plan: AgentPlan, registry: ToolRegistry, max_steps: int) -> AgentPlan:
    """Copy all arguments using the registry's validator before any dispatch."""
    if (
        type(plan) is not AgentPlan
        or type(plan.steps) is not tuple
        or not 1 <= len(plan.steps) <= min(max_steps, MAX_PLAN_STEPS)
    ):
        raise PlanError("malformed_plan")
    steps = []
    size = 0
    for step in plan.steps:
        if type(step) is not PlannedStep:
            raise PlanError("malformed_plan")
        try:
            definition = registry.describe(step.tool)
            arguments = validate_arguments(definition, step.arguments)
        except UnknownTool:
            raise PlanError("unknown_tool") from None
        except (InvalidArguments, RuntimeError):
            raise PlanError("invalid_arguments") from None
        size += len(json.dumps({"tool": definition.name, "arguments": arguments}))
        if size > MAX_PLAN_TEXT:
            raise PlanError("malformed_plan")
        steps.append(PlannedStep(definition.name, arguments))
    return AgentPlan(tuple(steps))


@dataclass(frozen=True, slots=True, repr=False)
class LLMPlanner:
    """One explicit provider request. No planning retries or observation release.

    Supply the instruction's tool vocabulary in trusted controller code. Only
    that instruction and a fixed format request leave the process.
    """

    provider: LLMProvider
    run_guardrails: RunGuardrails

    def __post_init__(self) -> None:
        self.check_run(self.run_guardrails)

    def check_run(self, run: RunGuardrails) -> None:
        if not isinstance(run, RunGuardrails) or self.run_guardrails is not run:
            raise ValueError("agent_run_mismatch")
        if type(self.provider) is not MockLLMProvider and (
            not isinstance(self.provider, GuardedProvider)
            or self.provider.run_guardrails is not run
        ):
            raise ValueError("agent_run_mismatch")

    def __call__(self, instruction: str) -> AgentPlan:
        self.check_run(self.run_guardrails)
        if (
            type(instruction) is not str
            or len(instruction) > MAX_INSTRUCTION
            or not instruction.strip()
        ):
            raise ValueError("invalid_task")
        run = self.run_guardrails
        run.cancellation.checkpoint()
        if run.budget.snapshot.blocked:
            raise AccountingBlocked()
        if run.budget.snapshot.remaining_steps <= 0:
            raise StepLimitExceeded()
        response = self.provider.generate(
            LLMRequest(
                (
                    LLMMessage(
                        "system",
                        'Return only JSON: {"steps":[{"tool":"name",'
                        '"arguments":{}}]}. Use only the tools specified in the task. '
                        "No extra fields, permissions, code, or completion claims. "
                        "Use 1-128 steps and at most 65536 characters.",
                    ),
                    LLMMessage("user", instruction),
                )
            )
        )
        run.cancellation.checkpoint()
        if run.budget.snapshot.blocked:
            raise AccountingBlocked()
        if type(response) is not LLMResponse:
            raise PlanError("malformed_plan")
        return parse_plan(response.content)
