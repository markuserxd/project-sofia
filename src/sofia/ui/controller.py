"""Trusted Qt controller: one owned worker, fresh run, no model authority."""

from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from sofia.core import AgentTask, TaskStatus
from sofia.persona import PersonaConfig
from sofia.providers import LLMMessage
from sofia.security import Action, RunGuardrails
from sofia.ui.approval import ApprovalCoordinator, MemoryFixture, Review
from sofia.ui.chat import ChatWork, compose_chat_request
from sofia.ui.models import (
    MAX_MESSAGE,
    MAX_TASKS,
    ChatStatus,
    Outcome,
    Preferences,
    Progress,
)
from sofia.ui.workers import Worker


@dataclass
class _Job:
    task: AgentTask
    thread: QThread
    worker: Worker
    coordinator: ApprovalCoordinator | None
    chat: ChatWork | None = None
    stopped: bool = False
    thread_done: bool = False
    outcome: Outcome | None = None
    last_chat_update: tuple[str, ChatStatus] | None = None


class Controller(QObject):
    busy_changed = Signal(bool)
    progress = Signal(object)
    chat_progress = Signal(str, object)
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
        self._turns: tuple[LLMMessage, ...] = ()
        self._chat_timer = QTimer(self)
        self._chat_timer.setInterval(40)
        self._chat_timer.timeout.connect(self._poll_chat)

    @property
    def busy(self) -> bool:
        return self._job is not None

    @property
    def real_chat_active(self) -> bool:
        return self._job is not None and self._job.chat is not None

    def clear_chat(self) -> None:
        if not self.real_chat_active:
            self._turns = ()

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
        persona = PersonaConfig(language=self.preferences.conversation_language)
        chat = None
        settings = self.preferences.chat
        if mode == "chat" and not settings.offline:
            if not settings.paid_enabled:
                self.error.emit("chat_consent")
                return False
            if not settings.credential_configured:
                self.error.emit("chat_missing_key")
                return False
            try:
                request = compose_chat_request(message, persona, self._turns)
            except UnicodeError:
                self.error.emit("invalid_message")
                return False
            except ValueError:
                self.error.emit("chat_context_limit")
                return False
            chat = ChatWork(settings, request)
            run = settings.new_run()
            # Only the trusted Send path consumes session consent for this exact scope.
            run.permissions.approve(Action.API_REQUEST, settings.scope)
        else:
            run = RunGuardrails(max_steps=8, max_tokens=0, max_usd=Decimal(0))
        task = AgentTask(
            "Run one paid conversation turn."
            if chat
            else "Run the fixed offline demonstration.",
            run,
        )
        coordinator = (
            ApprovalCoordinator(MemoryFixture(task)) if mode == "approval" else None
        )
        thread = QThread(self)
        worker = Worker(
            task,
            mode,
            message,
            coordinator,
            persona,
            chat,
        )
        worker.moveToThread(thread)
        self._job = _Job(task, thread, worker, coordinator, chat)
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
        if chat:
            self._chat_timer.start()
        else:
            self.progress.emit(Progress(task.task_id, task.status, run.budget.snapshot))
        thread.start()
        return True

    @Slot()
    def _poll_chat(self) -> None:
        job = self._job
        if job and job.chat and not job.stopped:
            update = (
                job.chat.stream.text,
                ChatStatus(job.chat.settings, job.task.run.budget.snapshot),
            )
            if update != job.last_chat_update:
                job.last_chat_update = update
                self.chat_progress.emit(*update)

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
        self._chat_timer.stop()
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
            reply="" if cancelled or outcome.cancelled else outcome.reply,
        )
        if job.chat:
            self._chat_timer.stop()
            self._poll_chat()
            outcome = replace(
                outcome,
                chat=ChatStatus(job.chat.settings, job.task.run.budget.snapshot),
            )
            if not outcome.cancelled and not outcome.error:
                self._turns += (
                    job.chat.request.messages[-1],
                    LLMMessage("assistant", outcome.reply),
                )
            job.task.run.permissions.revoke(Action.API_REQUEST, job.chat.settings.scope)
        if job.coordinator:
            job.coordinator.close()
        self.approval_closed.emit()
        self.history.append(replace(outcome, reply="") if job.chat else outcome)
        job.thread.deleteLater()
        self._job = None
        self.completed.emit(outcome)
        self.busy_changed.emit(False)
        self.idle.emit()

    def set_preferences(self, preferences: Preferences) -> None:
        # Called only after the settings dialog's Save click. Separate from any
        # active task. Paid chat controls stay locked until worker cleanup.
        if type(preferences) is not Preferences:
            raise ValueError("invalid_preferences")
        if self.real_chat_active:
            preferences = replace(preferences, chat=self.preferences.chat)
        scope = (
            f"ui:{preferences.theme}:{preferences.font_size}:{preferences.timestamps}"
        )
        run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
        run.permissions.approve(Action.SETTINGS_CHANGE, scope)
        try:
            run.begin_step(Action.SETTINGS_CHANGE, scope)
            if preferences.chat.route != self.preferences.chat.route:
                self.clear_chat()
            self.preferences = preferences
            self.preferences_changed.emit(preferences)
        finally:
            run.permissions.revoke(Action.SETTINGS_CHANGE, scope)
