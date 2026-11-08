from decimal import Decimal

import pytest
from PySide6.QtCore import QObject, QThread, QTimer, Slot

from sofia.core import (
    AgentLoop,
    AgentPlan,
    FailureCode,
    PlannedStep,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from sofia.providers import ProviderError
from sofia.security import AccountingBlocked, PermissionDenied
from sofia.ui.models import Preferences, Progress
from sofia.ui.workers import Worker


def test_real_loop_progress_budget_history_and_threads(window, wait, qapp, monkeypatch):
    calls = []
    actual = AgentLoop.execute

    def execute(loop, task):
        assert QThread.currentThread() is not qapp.thread()
        assert loop._registry.run_guardrails is task.run
        calls.append(task)
        return actual(loop, task)

    monkeypatch.setattr(AgentLoop, "execute", execute)

    class Receiver(QObject):
        def __init__(self):
            super().__init__()
            self.states = []

        @Slot(object)
        def record(self, progress):
            assert QThread.currentThread() is qapp.thread()
            self.states.append(progress.status)

    receiver = Receiver()
    window.controller.progress.connect(receiver.record)
    assert window.controller.start("task")
    assert not window.controller.start("task")
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1].result
    assert len(calls) == 1
    assert result.status is TaskStatus.COMPLETED
    assert result.attempts == result.completed_steps == result.budget.steps == 3
    assert result.budget.remaining_steps == 5
    assert result.budget.committed_tokens == result.budget.reserved_tokens == 0
    assert result.budget.committed_usd == result.budget.reserved_usd == Decimal(0)
    assert len(result.history) == 3
    assert all(
        record.tool == "mock_work" and record.verification is VerificationStatus.PASSED
        for record in result.history
    )
    assert receiver.states == [
        TaskStatus.CREATED,
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
    ]
    assert TaskStatus.VALIDATING in result.transitions
    assert TaskStatus.OBSERVING in result.transitions
    assert "Steps: 3 used / 5 remaining" in window.task_view.toPlainText()
    assert "verification passed" in window.task_view.toPlainText()


def test_slow_work_responsive_stop_and_fresh_run(window, wait):
    beats = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(True))
    timer.start()
    window.slow.click()
    old_job = window.controller._job
    wait(lambda: "running" in window.status.text())
    wait(lambda: len(beats) >= 4)
    assert window.settings_button.isEnabled() and window.stop.isEnabled()
    window.settings_button.click()
    assert window.settings_dialog.isVisible()
    window.controller.set_preferences(Preferences("Light"))
    window.stop.click()
    assert old_job.task.run.cancellation.cancelled
    assert window.controller.busy
    assert "Stopping" in window.status.text()
    assert not window.controller.start("task")
    wait(lambda: not window.controller.busy)
    timer.stop()
    outcome = window.controller.history[-1]
    assert outcome.result.status is TaskStatus.CANCELLED
    assert outcome.result.attempts == 1
    assert outcome.cancelled
    assert window.controller.start("task")
    new_task = window.controller._job.task
    assert new_task.run is not old_job.task.run
    assert new_task.run.budget.snapshot.steps == 0
    assert not new_task.run.cancellation.cancelled
    wait(lambda: not window.controller.busy)
    assert window.controller.history[-1].result.status is TaskStatus.COMPLETED


def test_late_stale_signals_and_thread_cleanup(window, wait):
    window.controller.start("task")
    job = window.controller._job
    thread = job.thread
    finished = []
    thread.finished.connect(lambda: finished.append(True))
    wait(lambda: not window.controller.busy)
    assert finished
    previous = window.controller.history[-1]
    window.controller.start("slow")
    current = window.controller._job
    window.controller._progress(
        Progress(previous.task_id, TaskStatus.COMPLETED, previous.result.budget)
    )
    window.controller._outcome(previous)
    assert window.controller._job is current
    assert current.outcome is None
    assert window.controller.busy
    window.controller.stop()
    wait(lambda: not window.controller.busy)


def test_native_cleanup_poll_is_nonblocking(window, wait, monkeypatch):
    actual_wait = QThread.wait
    probes = []

    def readiness(thread, milliseconds):
        assert milliseconds == 0
        probes.append(milliseconds)
        if len(probes) <= 3:
            assert window.controller.busy
            return False
        return actual_wait(thread, milliseconds)

    monkeypatch.setattr(QThread, "wait", readiness)
    window.demo.click()
    wait(lambda: not window.controller.busy)
    assert len(probes) >= 4
    assert window.controller.history[-1].result.status is TaskStatus.COMPLETED


def test_gui_approval_expiry_closes_without_effect(window, wait, monkeypatch):
    import sofia.ui.controller as controller
    from sofia.ui.approval import MemoryFixture

    monkeypatch.setattr(
        controller, "MemoryFixture", lambda task: MemoryFixture(task, lifetime=0.15)
    )
    window.protected.click()
    job = window.controller._job
    wait(lambda: not window.controller.busy)
    assert not job.coordinator.fixture.committed
    assert window.approval_dialog is None
    assert window.controller.history[-1].result.failure is FailureCode.PERMISSION_DENIED


def test_late_backend_success_cannot_announce_current_success(
    window, wait, monkeypatch
):
    from threading import Event

    ready, release = Event(), Event()
    actual = Worker.run_agent

    def finish_late(worker):
        result = actual(worker)
        ready.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(Worker, "run_agent", finish_late)
    window.controller.start("task")
    wait(ready.is_set)
    window.stop.click()
    assert "Stopping" in window.status.text()
    release.set()
    wait(lambda: not window.controller.busy)
    outcome = window.controller.history[-1]
    # Preserve the actual final AgentResult, while showing the stop-request race honestly.
    assert outcome.result.status is TaskStatus.COMPLETED
    assert outcome.cancelled
    assert window.status.text().startswith("Finished after Stop")


@pytest.mark.parametrize(
    "failure,code,status",
    [
        ("unknown", FailureCode.UNKNOWN_TOOL, TaskStatus.FAILED),
        ("arguments", FailureCode.INVALID_ARGUMENTS, TaskStatus.FAILED),
        ("planning", FailureCode.PLANNING_FAILED, TaskStatus.FAILED),
        ("provider", FailureCode.PROVIDER_FAILED, TaskStatus.FAILED),
        ("permission", FailureCode.PERMISSION_DENIED, TaskStatus.FAILED),
        ("verification", FailureCode.VERIFICATION_FAILED, TaskStatus.FAILED),
        ("steps", FailureCode.STEP_LIMIT, TaskStatus.LIMIT_EXCEEDED),
        ("accounting", FailureCode.ACCOUNTING_BLOCKED, TaskStatus.FAILED),
    ],
)
def test_core_failure_mapping(window, wait, monkeypatch, failure, code, status):
    import sofia.ui.workers as workers

    def loop(**kwargs):
        planner = kwargs["planner"]

        def fail(instruction):
            if failure == "unknown":
                return AgentPlan((PlannedStep("missing", {}),))
            if failure == "arguments":
                return AgentPlan((PlannedStep("mock_work", {"unexpected": True}),))
            if failure in ("planning", "provider", "permission", "accounting"):
                raise {
                    "planning": RuntimeError,
                    "provider": ProviderError,
                    "permission": PermissionDenied,
                    "accounting": AccountingBlocked,
                }[failure]()
            if failure == "steps":
                for _ in range(8):
                    kwargs["registry"].run_guardrails.budget.consume_step()
            return planner(instruction)

        kwargs["planner"] = fail
        if failure == "verification":
            kwargs["verifier"] = lambda *args: VerificationResult(False)
        return AgentLoop(**kwargs)

    monkeypatch.setattr(workers, "AgentLoop", loop)
    window.demo.click()
    wait(lambda: not window.controller.busy)
    result = window.controller.history[-1].result
    assert result.status is status and result.failure is code
    assert code.value in window.task_view.toPlainText()


def test_unexpected_worker_failure_and_privacy(window, wait, monkeypatch, caplog):
    private = "private-key-secret C:/Users/private/folder"

    def fail(worker):
        raise RuntimeError(private)

    monkeypatch.setattr(Worker, "run_agent", fail)
    window.message.setPlainText(private)
    window.demo.click()
    wait(lambda: not window.controller.busy)
    assert window.controller.history[-1].error == "worker_failed"
    assert private not in caplog.text
    assert private not in window.task_view.toPlainText()
    assert "worker_failed" in window.status.text()


def test_approval_payload_not_logged(window, wait, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="sofia")
    window.protected.click()
    wait(lambda: window.approval_dialog is not None)
    review = window.approval_dialog.review
    window.approval_dialog.approve.click()
    wait(lambda: not window.controller.busy)
    assert review.payload not in caplog.text
    assert review.scope not in caplog.text
    assert review.preview_id not in caplog.text
    assert review.payload not in window.task_view.toPlainText()
    assert "memory_commit" in caplog.text


def test_close_while_worker_running_does_not_destroy_thread(window, wait):
    window.slow.click()
    job = window.controller._job
    window.close()
    assert job.task.run.cancellation.cancelled
    wait(lambda: not window.controller.busy and not window.isVisible())
    assert window.controller.history[-1].cancelled
    assert not window.controller.start("task")


def test_late_chat_reply_is_discarded(window, wait, monkeypatch):
    from threading import Event

    from sofia.providers import LLMResponse, MockLLMProvider

    ready, release = Event(), Event()

    def slow_response(self, request):
        ready.set()
        assert release.wait(2)
        return LLMResponse("Must not show")

    monkeypatch.setattr(MockLLMProvider, "generate", slow_response)
    window.controller.start("chat", "Hello")
    wait(ready.is_set)
    window.stop.click()
    release.set()
    wait(lambda: not window.controller.busy)
    assert window.controller.history[-1].cancelled
    assert not window.controller.history[-1].reply
    assert "Must not show" not in window.status.text()
