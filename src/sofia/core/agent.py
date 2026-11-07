"""Sequential, bounded orchestration over the existing guarded tool boundary."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from uuid import uuid4

from sofia.core.planning import (
    MAX_INSTRUCTION,
    LLMPlanner,
    PlanError,
    PlannedStep,
    Planner,
    validate_plan,
)
from sofia.core.state import TaskState, TaskStatus
from sofia.providers.base import ProviderError
from sofia.security import (
    AccountingBlocked,
    BudgetSnapshot,
    GuardrailError,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    SpendingBudgetExceeded,
    StepLimitExceeded,
    TokenBudgetExceeded,
)
from sofia.tools import JSONValue, ToolRegistry, ToolResult
from sofia.tools.schemas import InvalidArguments, copy_json, valid_name


class FailureCode(Enum):
    PLANNING_FAILED = "planning_failed"
    MALFORMED_PLAN = "malformed_plan"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    VERIFICATION_FAILED = "verification_failed"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    STEP_LIMIT = "step_limit"
    TOKEN_LIMIT = "token_limit"
    SPENDING_LIMIT = "spending_limit"
    ATTEMPT_LIMIT = "attempt_limit"
    ITERATION_LIMIT = "iteration_limit"
    HISTORY_LIMIT = "history_limit"
    PROVIDER_FAILED = "provider_failed"
    ACCOUNTING_BLOCKED = "accounting_blocked"
    GUARDRAIL_DENIED = "guardrail_denied"
    RUN_MISMATCH = "run_mismatch"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_plan_steps: int = 16
    max_attempts: int = 32
    max_retries_per_step: int = 0
    max_iterations: int = 128
    max_history: int = 32

    def __post_init__(self) -> None:
        for value, minimum, maximum in (
            (self.max_plan_steps, 1, 128),
            (self.max_attempts, 1, 1024),
            (self.max_retries_per_step, 0, 3),
            (self.max_iterations, 1, 4096),
            (self.max_history, 1, 1024),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid_agent_config")


@dataclass(frozen=True, slots=True)
class AgentTask:
    instruction: str = field(repr=False)
    run: RunGuardrails = field(repr=False)
    config: AgentConfig = field(default_factory=AgentConfig)
    task_id: str = field(default_factory=lambda: uuid4().hex, init=False)
    _state: TaskState = field(default_factory=TaskState, init=False, repr=False)
    _claim_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.instruction) is not str
            or len(self.instruction) > MAX_INSTRUCTION
            or not self.instruction.strip()
            or not isinstance(self.run, RunGuardrails)
            or type(self.config) is not AgentConfig
        ):
            raise ValueError("invalid_task")

    @property
    def status(self) -> TaskStatus:
        return self._state.status

    def _claim(self) -> None:
        # Never released: this object admits exactly one execution, even after
        # process-level interruption or a competing call through another loop.
        if not self._claim_lock.acquire(blocking=False):
            raise ValueError("task_already_executed")


@dataclass(frozen=True, slots=True)
class Observation:
    """Private in-memory data for the trusted verifier, never audit metadata."""

    output: JSONValue = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    retry_safe: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.passed) is not bool
            or type(self.retry_safe) is not bool
            or (self.passed and self.retry_safe)
        ):
            raise ValueError("invalid_verification")


Verifier = Callable[[PlannedStep, Observation], VerificationResult]


class ExecutionStatus(Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class VerificationStatus(Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepRecord:
    step_id: int
    tool: str
    attempt: int
    execution: ExecutionStatus
    verification: VerificationStatus
    failure: FailureCode | None
    state: TaskStatus
    invocation_id: str | None


@dataclass(frozen=True, slots=True)
class AgentResult:
    task_id: str
    status: TaskStatus
    completed_steps: int
    attempts: int
    planner_attempts: int
    iterations: int
    failure: FailureCode | None
    history: tuple[StepRecord, ...]
    transitions: tuple[TaskStatus, ...]
    budget: BudgetSnapshot


CompletionVerifier = Callable[[tuple[StepRecord, ...]], VerificationResult]


class _Stop(Exception):
    def __init__(self, code: FailureCode) -> None:
        self.code = code
        super().__init__(code.value)


_LIMITS = frozenset(
    (
        FailureCode.STEP_LIMIT,
        FailureCode.TOKEN_LIMIT,
        FailureCode.SPENDING_LIMIT,
        FailureCode.ATTEMPT_LIMIT,
        FailureCode.ITERATION_LIMIT,
        FailureCode.HISTORY_LIMIT,
    )
)


def _failure(error: BaseException, task: AgentTask) -> FailureCode:
    # A latched stop takes precedence over a callback's reassuring return/error.
    if task.run.cancellation.cancelled:
        return FailureCode.CANCELLED
    if task.run.budget.snapshot.blocked:
        return FailureCode.ACCOUNTING_BLOCKED
    if type(error) is _Stop:
        return error.code
    if isinstance(error, OperationCancelled):
        return FailureCode.CANCELLED
    codes: dict[type[BaseException], FailureCode] = {
        PermissionDenied: FailureCode.PERMISSION_DENIED,
        StepLimitExceeded: FailureCode.STEP_LIMIT,
        TokenBudgetExceeded: FailureCode.TOKEN_LIMIT,
        SpendingBudgetExceeded: FailureCode.SPENDING_LIMIT,
        AccountingBlocked: FailureCode.ACCOUNTING_BLOCKED,
    }
    if type(error) in codes:
        return codes[type(error)]
    if isinstance(error, GuardrailError):
        return FailureCode.GUARDRAIL_DENIED
    if isinstance(error, ProviderError):
        return FailureCode.PROVIDER_FAILED
    if type(error) is PlanError:
        reason = (
            error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else ""
        )
        return {
            "malformed_plan": FailureCode.MALFORMED_PLAN,
            "unknown_tool": FailureCode.UNKNOWN_TOOL,
            "invalid_arguments": FailureCode.INVALID_ARGUMENTS,
        }.get(reason, FailureCode.MALFORMED_PLAN)
    return {
        TaskStatus.PLANNING: FailureCode.PLANNING_FAILED,
        TaskStatus.VALIDATING: FailureCode.MALFORMED_PLAN,
        TaskStatus.VERIFYING: FailureCode.VERIFICATION_FAILED,
    }.get(task.status, FailureCode.TOOL_FAILED)


class AgentLoop:
    """Trusted dependencies only; the plan cannot configure any execution policy."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        planner: Planner,
        verifier: Verifier,
        completion_verifier: CompletionVerifier,
        retry_tools: frozenset[str] = frozenset(),
    ) -> None:
        if (
            not isinstance(registry, ToolRegistry)
            or not callable(planner)
            or not callable(verifier)
            or not callable(completion_verifier)
            or type(retry_tools) is not frozenset
            or len(retry_tools) > 128
            or any(
                not valid_name(name) or not registry.contains(name)
                for name in retry_tools
            )
        ):
            raise ValueError("invalid_agent_config")
        if isinstance(planner, LLMPlanner):
            planner.check_run(registry.run_guardrails)
        self._registry = registry
        self._planner = planner
        self._verifier = verifier
        self._completion_verifier = completion_verifier
        self._retry_tools = retry_tools

    def execute(self, task: AgentTask) -> AgentResult:
        if type(task) is not AgentTask:
            raise ValueError("invalid_task")
        task._claim()
        return _Execution(self, task).execute()


class _Execution:
    """Per-invocation counters; no mutable history is shared across tasks."""

    def __init__(self, loop: AgentLoop, task: AgentTask) -> None:
        self.loop = loop
        self.task = task
        self.history: list[StepRecord] = []
        self.transitions = [task.status]
        self.attempts = 0
        self.completed = 0
        self.planner_attempts = 0
        self.iterations = 0

    def audit(self, event: str, **metadata: str | int | None) -> None:
        # All callers supply constants, generated IDs or validated registry names.
        try:
            logging.getLogger("sofia.agent").info(
                json.dumps({"event": event, "task_id": self.task.task_id, **metadata})
            )
        except Exception:
            pass

    def checkpoint(self, *, scheduling: bool = False) -> None:
        run = self.task.run
        run.cancellation.checkpoint()
        snapshot = run.budget.snapshot
        if snapshot.blocked:
            raise AccountingBlocked()
        if scheduling and snapshot.remaining_steps <= 0:
            raise StepLimitExceeded()

    def iteration(self) -> None:
        self.checkpoint()
        if self.iterations >= self.task.config.max_iterations:
            raise _Stop(FailureCode.ITERATION_LIMIT)
        self.iterations += 1

    def transition(self, status: TaskStatus) -> None:
        if not status.terminal:
            self.iteration()
        self.task._state.transition(status)
        self.transitions.append(status)
        self.audit("agent_transition", state=status.value)

    def execute(self) -> AgentResult:
        code = None
        self.audit("agent_started")
        try:
            self.checkpoint(scheduling=True)
            if self.loop._registry.run_guardrails is not self.task.run:
                raise _Stop(FailureCode.RUN_MISMATCH)
            if isinstance(self.loop._planner, LLMPlanner):
                self.loop._planner.check_run(self.task.run)
            self.transition(TaskStatus.PLANNING)
            self.checkpoint(scheduling=True)
            self.planner_attempts = 1
            plan = self.loop._planner(self.task.instruction)
            self.checkpoint()
            self.transition(TaskStatus.VALIDATING)
            self.checkpoint()
            plan = validate_plan(
                plan, self.loop._registry, self.task.config.max_plan_steps
            )
            self.checkpoint()
            self.audit("agent_plan_validated", steps=len(plan.steps))
            for index, step in enumerate(plan.steps, 1):
                self.execute_step(index, step)
            self.iteration()
            completion = self.loop._completion_verifier(tuple(self.history))
            self.checkpoint()
            if type(completion) is not VerificationResult or not completion.passed:
                self.audit("agent_completion_verification", status="failed")
                raise _Stop(FailureCode.VERIFICATION_FAILED)
            self.audit("agent_completion_verification", status="passed")
            self.checkpoint()
            self.transition(TaskStatus.COMPLETED)
        except Exception as error:
            code = _failure(error, self.task)
            if self.task.status in (TaskStatus.PLANNING, TaskStatus.VALIDATING):
                self.audit("agent_plan_rejected", failure=code.value)
            terminal = (
                TaskStatus.CANCELLED
                if code is FailureCode.CANCELLED
                else TaskStatus.LIMIT_EXCEEDED
                if code in _LIMITS
                else TaskStatus.FAILED
            )
            self.transition(terminal)
        except BaseException:
            self.transition(TaskStatus.FAILED)
            self.audit("agent_stopped", failure=FailureCode.INTERRUPTED.value)
            raise
        self.audit(
            "agent_stopped",
            state=self.task.status.value,
            failure=code.value if code else None,
        )
        return AgentResult(
            self.task.task_id,
            self.task.status,
            self.completed,
            self.attempts,
            self.planner_attempts,
            self.iterations,
            code,
            tuple(self.history),
            tuple(self.transitions),
            self.task.run.budget.snapshot,
        )

    def execute_step(self, index: int, step: PlannedStep) -> None:
        for attempt in range(1, self.task.config.max_retries_per_step + 2):
            self.checkpoint(scheduling=True)
            if self.attempts >= self.task.config.max_attempts:
                raise _Stop(FailureCode.ATTEMPT_LIMIT)
            # Reserve a record before dispatch; never silently truncate history.
            if len(self.history) >= self.task.config.max_history:
                raise _Stop(FailureCode.HISTORY_LIMIT)
            self.transition(TaskStatus.RUNNING)
            self.checkpoint(scheduling=True)
            self.attempts += 1
            self.audit(
                "agent_step_started", step=index, tool=step.tool, attempt=attempt
            )
            execution = ExecutionStatus.INTERRUPTED
            verification = VerificationStatus.NOT_RUN
            invocation = None
            failure = None
            decision = VerificationResult(False)
            try:
                result = self.loop._registry.execute(step.tool, step.arguments)
                if type(result) is not ToolResult:
                    raise _Stop(FailureCode.INVALID_OUTPUT)
                # Registry IDs are opaque hex. Do not echo a malformed boundary result.
                if (
                    type(result.invocation_id) is not str
                    or len(result.invocation_id) != 32
                    or any(c not in "0123456789abcdef" for c in result.invocation_id)
                    or result.tool != step.tool
                ):
                    raise _Stop(FailureCode.INVALID_OUTPUT)
                invocation = result.invocation_id
                execution = (
                    ExecutionStatus.SUCCEEDED
                    if result.succeeded
                    else ExecutionStatus.FAILED
                )
                self.checkpoint()
                self.transition(TaskStatus.OBSERVING)
                if not result.succeeded:
                    assert result.error is not None
                    raise _Stop(
                        {
                            "timeout": FailureCode.TIMEOUT,
                            "invalid_output": FailureCode.INVALID_OUTPUT,
                            "unknown_tool": FailureCode.UNKNOWN_TOOL,
                            "invalid_arguments": FailureCode.INVALID_ARGUMENTS,
                        }.get(result.error, FailureCode.TOOL_FAILED)
                    )
                try:
                    observation = Observation(copy_json(result.output))
                except (InvalidArguments, RuntimeError):
                    raise _Stop(FailureCode.INVALID_OUTPUT) from None
                self.transition(TaskStatus.VERIFYING)
                # The verifier cannot mutate validated arguments for later attempts.
                arguments = copy_json(step.arguments)
                assert isinstance(arguments, dict)
                self.checkpoint()
                decision = self.loop._verifier(
                    PlannedStep(step.tool, arguments), observation
                )
                if type(decision) is not VerificationResult:
                    raise _Stop(FailureCode.VERIFICATION_FAILED)
                verification = (
                    VerificationStatus.PASSED
                    if decision.passed
                    else VerificationStatus.FAILED
                )
                self.audit("agent_verification", step=index, status=verification.value)
                self.checkpoint()
                if decision.passed:
                    self.completed += 1
                else:
                    failure = FailureCode.VERIFICATION_FAILED
            except BaseException as error:
                failure = (
                    _failure(error, self.task)
                    if isinstance(error, Exception)
                    else FailureCode.INTERRUPTED
                )
                if (
                    self.task.status is TaskStatus.VERIFYING
                    and verification is VerificationStatus.NOT_RUN
                ):
                    verification = VerificationStatus.FAILED
                    self.audit(
                        "agent_verification", step=index, status=verification.value
                    )
                self.audit("agent_retry_refused", step=index)
                raise
            finally:
                self.history.append(
                    StepRecord(
                        index,
                        step.tool,
                        attempt,
                        execution,
                        verification,
                        failure,
                        self.task.status,
                        invocation,
                    )
                )
                self.audit(
                    "agent_step_finished",
                    step=index,
                    attempt=attempt,
                    execution=execution.value,
                    verification=verification.value,
                    failure=failure.value if failure else None,
                    invocation_id=invocation,
                )
            if decision.passed:
                return
            self.checkpoint()
            if (
                decision.retry_safe
                and step.tool in self.loop._retry_tools
                and attempt <= self.task.config.max_retries_per_step
            ):
                self.audit("agent_retry_attempted", step=index, attempt=attempt + 1)
                continue
            self.audit("agent_retry_refused", step=index)
            raise _Stop(FailureCode.VERIFICATION_FAILED)
