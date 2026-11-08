"""Trusted Qt controller: one owned worker, fresh run, no model authority."""

from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from sofia.core import AgentTask, TaskStatus
from sofia.security import Action, RunGuardrails
from sofia.ui.approval import ApprovalCoordinator, MemoryFixture, Review
from sofia.ui.models import MAX_MESSAGE, MAX_TASKS, Outcome, Preferences, Progress
from sofia.ui.workers import Worker


@dataclass
class _Job:
    task: AgentTask
    thread: QThread
    worker: Worker
    coordinator: ApprovalCoordinator | None
    stopped: bool = False
    thread_done: bool = False
    outcome: Outcome | None = None


class Controller(QObject):
    busy_changed = Signal(bool)
    progress = Signal(object)
    approval_requested = Signal(object)
    approval_closed = Signal()
    completed = Signal(object)
    stopping = Signal()
    error = Signal(str)
    preferences_changed = Signal(object)
    idle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._job: _Job | None = None
        self._closing = False
        self.history: deque[Outcome] = deque(maxlen=MAX_TASKS)
        self.preferences = Preferences()

    @property
    def busy(self) -> bool:
        return self._job is not None

    def start(self, mode: str, message: str = "") -> bool:
        if self.busy or self._closing:
            return False
        if mode not in ("chat", "task", "approval", "slow"):
            self.error.emit("invalid_request")
            return False
        if mode == "chat" and (
            type(message) is not str
            or not message.strip()
            or len(message) > MAX_MESSAGE
        ):
            self.error.emit("invalid_message")
            return False
        run = RunGuardrails(max_steps=8, max_tokens=0, max_usd=Decimal(0))
        task = AgentTask("Run the fixed offline demonstration.", run)
        coordinator = (
            ApprovalCoordinator(MemoryFixture(task)) if mode == "approval" else None
        )
        thread = QThread(self)
        worker = Worker(task, mode, message, coordinator)
        worker.moveToThread(thread)
        self._job = _Job(task, thread, worker, coordinator)
        thread.started.connect(worker.execute)
        worker.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        worker.approval.connect(self._approval, Qt.ConnectionType.QueuedConnection)
        worker.outcome.connect(self._outcome, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        # quit is thread-safe; it must not depend on servicing the GUI queue.
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(
            self._thread_finished, Qt.ConnectionType.QueuedConnection
        )
        self.busy_changed.emit(True)
        self.progress.emit(Progress(task.task_id, task.status, run.budget.snapshot))
        thread.start()
        return True

    @Slot(object)
    def _progress(self, snapshot: Progress) -> None:
        job = self._job
        if job and snapshot.task_id == job.task.task_id and not job.stopped:
            self.progress.emit(snapshot)

    @Slot(object)
    def _approval(self, review: Review) -> None:
        job = self._job
        if (
            job
            and not job.stopped
            and not self._closing
            and job.coordinator
            and review is job.coordinator.fixture.review
        ):
            self.approval_requested.emit(review)

    def decide(self, review: Review, approved: bool) -> bool:
        job = self._job
        return bool(
            job
            and not job.stopped
            and not self._closing
            and job.coordinator
            and job.coordinator.decide(review, approved)
        )

    @Slot()
    def stop(self) -> None:
        job = self._job
        if job is None or job.stopped:
            return
        job.stopped = True
        job.task.run.cancellation.cancel()
        if job.coordinator:
            job.coordinator.close()
        self.approval_closed.emit()
        self.stopping.emit()

    def shutdown(self) -> None:
        already_closing = self._closing
        self._closing = True
        self.stop()
        if not self.busy and not already_closing:
            self.idle.emit()

    @Slot(object)
    def _outcome(self, outcome: Outcome) -> None:
        job = self._job
        if (
            job is None
            or outcome.task_id != job.task.task_id
            or job.outcome is not None
        ):
            return
        job.outcome = outcome
        QTimer.singleShot(0, self._finish_if_ready)

    @Slot()
    def _thread_finished(self) -> None:
        job = self._job
        if job and self.sender() is job.thread:
            job.thread_done = True
            # Render after signal delivery unwinds, outside QThread finalization.
            QTimer.singleShot(0, self._finish_if_ready)

    def _finish_if_ready(self) -> None:
        job = self._job
        if job is None or not job.thread_done or job.outcome is None:
            return
        # finished() precedes native thread-local cleanup. Never render or delete
        # a thread while that cleanup is still running. A zero-time join only
        # checks readiness; retry through the event loop without blocking Stop.
        if not job.thread.wait(0):
            QTimer.singleShot(5, self._finish_if_ready)
            return
        outcome = job.outcome
        cancelled = job.stopped or (
            outcome.result is not None and outcome.result.status is TaskStatus.CANCELLED
        )
        outcome = replace(
            outcome,
            cancelled=cancelled or outcome.cancelled,
            reply="" if cancelled else outcome.reply,
        )
        if job.coordinator:
            job.coordinator.close()
        self.approval_closed.emit()
        self.history.append(outcome)
        job.thread.deleteLater()
        self._job = None
        self.completed.emit(outcome)
        self.busy_changed.emit(False)
        self.idle.emit()

    def set_preferences(self, preferences: Preferences) -> None:
        # Called only after the settings dialog's Save click. Separate from any
        # active task; preferences cannot alter providers, permissions or limits.
        if type(preferences) is not Preferences:
            raise ValueError("invalid_preferences")
        scope = (
            f"ui:{preferences.theme}:{preferences.font_size}:{preferences.timestamps}"
        )
        run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
        run.permissions.approve(Action.SETTINGS_CHANGE, scope)
        try:
            run.begin_step(Action.SETTINGS_CHANGE, scope)
            self.preferences = preferences
            self.preferences_changed.emit(preferences)
        finally:
            run.permissions.revoke(Action.SETTINGS_CHANGE, scope)
