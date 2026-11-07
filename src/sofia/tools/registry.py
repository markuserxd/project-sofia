"""Explicit trusted registration and a single synchronous execution boundary."""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4

from sofia.security import (
    AccountingBlocked,
    Cancellation,
    GuardrailError,
    InvalidGuardrailInput,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    StepLimitExceeded,
)
from sofia.security.permissions import Action
from sofia.tools.schemas import (
    InvalidArguments,
    InvalidToolDefinition,
    JSONValue,
    ToolDefinition,
    copy_json,
    valid_name,
    validate_arguments,
    validate_definition,
)

ToolErrorCode = Literal[
    "unknown_tool", "invalid_arguments", "timeout", "handler_failed", "invalid_output"
]


class DuplicateTool(ValueError):
    def __init__(self) -> None:
        super().__init__("duplicate_tool")


class UnknownTool(LookupError):
    def __init__(self) -> None:
        super().__init__("unknown_tool")


class ToolTimeout(Exception):
    def __init__(self) -> None:
        super().__init__("tool_timeout")


@dataclass(frozen=True, slots=True, repr=False)
class ToolContext:
    """Cooperative checks only; no permission granting or accounting access."""

    _cancellation: Cancellation
    deadline: float
    _clock: Callable[[], float]

    def checkpoint(self) -> None:
        self._cancellation.checkpoint()
        if self._clock() >= self.deadline:
            raise ToolTimeout()


class ToolHandler(Protocol):
    def __call__(
        self, arguments: Mapping[str, JSONValue], context: ToolContext, /
    ) -> JSONValue: ...


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str | None
    invocation_id: str
    output: JSONValue = field(default=None, repr=False)
    error: ToolErrorCode | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True, repr=False)
class _RegisteredTool:
    definition: ToolDefinition
    handler: ToolHandler
    action: Action | None
    target: str | None


class ToolRegistry:
    """One existing run, trusted handlers, no workers or implicit approvals.

    Registration is for application code only. Handlers are trusted Python,
    not sandboxed. A controller may expose discovery metadata to a model but
    must retain ownership of registration and execution.
    """

    def __init__(
        self, *, run: RunGuardrails, clock: Callable[[], float] = monotonic
    ) -> None:
        if not isinstance(run, RunGuardrails) or not callable(clock):
            raise InvalidToolDefinition()
        self._run = run
        self._clock = clock
        self._tools: dict[str, _RegisteredTool] = {}
        self._lock = Lock()

    @property
    def run_guardrails(self) -> RunGuardrails:
        """Actual shared run identity for trusted orchestration controllers."""
        return self._run

    def register(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
        *,
        action: Action | None = None,
        target: str | None = None,
    ) -> None:
        validate_definition(definition)
        if not callable(handler):
            raise InvalidToolDefinition()
        if action is None:
            if target is not None:
                raise InvalidToolDefinition()
        elif (
            type(action) is not Action
            or action is Action.API_REQUEST
            or type(target) is not str
            or not target.strip()
            or target == "*"
        ):
            raise InvalidToolDefinition()
        with self._lock:
            if definition.name in self._tools:
                raise DuplicateTool()
            self._tools[definition.name] = _RegisteredTool(
                definition, handler, action, target
            )

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        with self._lock:
            return tuple(entry.definition for entry in self._tools.values())

    def contains(self, name: str) -> bool:
        with self._lock:
            return valid_name(name) and name in self._tools

    def describe(self, name: str) -> ToolDefinition:
        return self._resolve(name).definition

    def _resolve(self, name: str) -> _RegisteredTool:
        with self._lock:
            if not valid_name(name) or name not in self._tools:
                raise UnknownTool()
            return self._tools[name]

    def _checkpoint(self) -> None:
        self._run.cancellation.checkpoint()
        if self._run.budget.snapshot.blocked:
            raise AccountingBlocked()

    def execute(self, name: str, arguments: object) -> ToolResult:
        """Return tool failures; propagate existing guardrail/process exceptions.

        Every attempt emits one final sanitized audit event when control returns.
        Failed dispatch checks spend no step. Once consumed, a step is never
        refunded, including handler errors, cancellation and deadline expiry.
        """
        invocation_id = uuid4().hex
        safe_name = None
        status = "interrupted"
        authorized = False
        try:
            self._checkpoint()
            try:
                entry = self._resolve(name)
            except UnknownTool:
                status = "unknown_tool"
                return ToolResult(None, invocation_id, error="unknown_tool")
            safe_name = entry.definition.name
            try:
                validated = validate_arguments(entry.definition, arguments)
            except (InvalidArguments, RuntimeError):
                # Concurrent mutation of caller-owned containers also fails closed.
                status = "invalid_arguments"
                return ToolResult(safe_name, invocation_id, error="invalid_arguments")
            context = ToolContext(
                self._run.cancellation,
                self._clock() + entry.definition.timeout,
                self._clock,
            )
            try:
                context.checkpoint()
                if entry.action is None:
                    self._run.budget.consume_step()
                else:
                    assert entry.target is not None
                    self._run.begin_step(entry.action, entry.target)
                authorized = True
                context.checkpoint()
                try:
                    output = entry.handler(validated, context)
                except (GuardrailError, ToolTimeout):
                    raise
                except Exception:
                    self._checkpoint()
                    context.checkpoint()
                    status = "handler_failed"
                    return ToolResult(safe_name, invocation_id, error="handler_failed")
                self._checkpoint()
                context.checkpoint()
                try:
                    isolated_output = copy_json(output)
                except (InvalidArguments, RuntimeError):
                    status = "invalid_output"
                    return ToolResult(safe_name, invocation_id, error="invalid_output")
                self._checkpoint()
                context.checkpoint()
            except ToolTimeout:
                self._checkpoint()
                status = "timeout"
                return ToolResult(safe_name, invocation_id, error="timeout")
            status = "success"
            return ToolResult(safe_name, invocation_id, output=isolated_output)
        except GuardrailError as error:
            # Never trust arbitrary exception strings or subclass-defined codes.
            status = {
                PermissionDenied: "permission_denied",
                OperationCancelled: "operation_cancelled",
                StepLimitExceeded: "step_limit_exceeded",
                AccountingBlocked: "accounting_blocked",
                InvalidGuardrailInput: "invalid_guardrail_input",
            }.get(type(error), "guardrail_denied")
            raise
        finally:
            self._audit(invocation_id, safe_name, status, authorized)

    @staticmethod
    def _audit(
        invocation_id: str, tool: str | None, status: str, authorized: bool
    ) -> None:
        try:
            logging.getLogger("sofia.tools").info(
                json.dumps(
                    {
                        "event": "tool_execution",
                        "invocation_id": invocation_id,
                        "tool": tool,
                        "status": status,
                        "authorized": authorized,
                    }
                )
            )
        except Exception:
            # Logging failure never changes permissions, accounting or results.
            pass
