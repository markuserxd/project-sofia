"""Synchronous core work runs on a QObject owned by a QThread, never widgets."""

import logging
from collections.abc import Mapping
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from sofia.core import (
    AgentLoop,
    AgentPlan,
    AgentResult,
    AgentTask,
    Observation,
    PlannedStep,
    StepRecord,
    VerificationResult,
    VerificationStatus,
)
from sofia.logging_setup import task_log_context
from sofia.providers import LLMMessage, LLMRequest, MockLLMProvider
from sofia.security import OperationCancelled
from sofia.tools import JSONValue, ToolContext, ToolDefinition, ToolRegistry
from sofia.ui.approval import ApprovalCoordinator
from sofia.ui.models import MAX_MESSAGE, Outcome, Progress


class Worker(QObject):
    progress = Signal(object)
    approval = Signal(object)
    outcome = Signal(object)
    finished = Signal()

    def __init__(
        self,
        task: AgentTask,
        mode: str,
        message: str,
        coordinator: ApprovalCoordinator | None,
    ) -> None:
        super().__init__()
        self.task = task
        self.mode = mode
        self.message = message
        self.coordinator = coordinator

    def report(self) -> None:
        self.progress.emit(
            Progress(self.task.task_id, self.task.status, self.task.run.budget.snapshot)
        )

    @Slot()
    def execute(self) -> None:
        with task_log_context(self.task.task_id):
            try:
                self.task.run.cancellation.checkpoint()
                if self.mode == "chat":
                    response = MockLLMProvider(
                        response_text=(
                            "สวัสดี! Hi, I'm Sofia. This is an offline mock reply. "
                            "Try a safe task or review a synthetic approval — no API bill attached."
                        )
                    ).generate(LLMRequest((LLMMessage("user", self.message),)))
                    self.task.run.cancellation.checkpoint()
                    outcome = Outcome(
                        self.task.task_id, reply=response.content[:MAX_MESSAGE]
                    )
                else:
                    outcome = Outcome(self.task.task_id, result=self.run_agent())
            except OperationCancelled:
                outcome = Outcome(self.task.task_id, cancelled=True)
            except BaseException:
                # Do not interpolate exceptions, input, paths or review payloads.
                logging.getLogger("sofia.ui").error("ui_worker_failed")
                outcome = Outcome(self.task.task_id, error="worker_failed")
            finally:
                if self.coordinator is not None:
                    self.coordinator.close()
            self.outcome.emit(outcome)
            self.finished.emit()

    def run_agent(self) -> AgentResult:
        registry = ToolRegistry(run=self.task.run)
        observed: list[int] = []

        def work(arguments: Mapping[str, JSONValue], context: ToolContext) -> JSONValue:
            self.report()  # Actual RUNNING state inside a dispatched handler.
            # Bounded synthetic delay; cooperative checkpoints in the worker only.
            for _ in range(100 if self.mode == "slow" else 4):
                context.checkpoint()
                Event().wait(0.02)
            value = len(observed) + 1
            observed.append(value)
            return value

        registry.register(ToolDefinition("mock_work", "Bounded in-memory work"), work)
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.fixture.register(registry, self.report)
        expected = 1 if coordinator else 3

        def plan(instruction: str) -> AgentPlan:
            self.report()  # Actual PLANNING state inside the trusted planner.
            if coordinator is not None:
                coordinator.wait(self.approval.emit)
                return AgentPlan(
                    (
                        PlannedStep(
                            "memory_commit",
                            {"preview_id": coordinator.fixture.review.preview_id},
                        ),
                    )
                )
            return AgentPlan(
                tuple(PlannedStep("mock_work", {}) for _ in range(expected))
            )

        def verify(step: PlannedStep, observation: Observation) -> VerificationResult:
            self.report()  # Actual VERIFYING state, no raw observations emitted.
            passed = (
                (
                    coordinator.fixture.committed
                    and observation.output == {"committed": True}
                )
                if coordinator
                else (observation.output == len(observed) and len(observed) <= expected)
            )
            return VerificationResult(passed)

        def complete(records: tuple[StepRecord, ...]) -> VerificationResult:
            return VerificationResult(
                len(records) == expected
                and all(r.verification is VerificationStatus.PASSED for r in records)
                and (
                    coordinator.fixture.committed
                    if coordinator
                    else observed == list(range(1, expected + 1))
                )
            )

        return AgentLoop(
            registry=registry,
            planner=plan,
            verifier=verify,
            completion_verifier=complete,
        ).execute(self.task)
