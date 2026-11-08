"""A small desktop shell; widgets cannot register tools or grant capabilities."""

from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sofia.core import TaskStatus
from sofia.ui.approval import Review
from sofia.ui.controller import Controller
from sofia.ui.dialogs import ApprovalDialog, SettingsDialog
from sofia.ui.models import MAX_TASKS, Outcome, Preferences, Progress
from sofia.ui.themes import stylesheet
from sofia.ui.views import Conversation, MessageInput, TaskView, label


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.controller = Controller(self)
        self.approval_dialog: ApprovalDialog | None = None
        self.settings_dialog: SettingsDialog | None = None
        self._closing = False
        self.setWindowTitle("SOFIA · Your offline assistant")
        self.resize(1200, 780)
        self.setMinimumSize(840, 600)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(label("SOFIA", "heading"))
        header.addWidget(label("A little help. A little more calm.", "muted"))
        header.addStretch()
        self.mode = label("● Offline Mock", "muted")
        header.addWidget(self.mode)
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self._settings)
        header.addWidget(self.settings_button)
        outer.addLayout(header)

        splitter = QSplitter()
        outer.addWidget(splitter, 1)
        sidebar = QFrame()
        sidebar.setObjectName("panel")
        sidebar.setMinimumWidth(150)
        side = QVBoxLayout(sidebar)
        self.new_chat = QPushButton("New chat")
        self.new_chat.clicked.connect(self._new_chat)
        side.addWidget(self.new_chat)
        side.addWidget(label("RECENT TASKS", "muted"))
        self.tasks = QListWidget()
        self.tasks.setAccessibleName("Recent task results; last twenty runs")
        self.tasks.currentRowChanged.connect(self._show_history)
        side.addWidget(self.tasks, 1)
        side.addWidget(label("Private by default\nHistory stays in memory.", "muted"))
        splitter.addWidget(sidebar)

        middle = QWidget()
        conversation_layout = QVBoxLayout(middle)
        conversation_layout.setContentsMargins(8, 0, 8, 0)
        self.conversation = Conversation()
        conversation_layout.addWidget(self.conversation, 1)
        self.status = label("Idle · Ready when you are", "muted")
        self.status.setAccessibleName("Current execution status")
        conversation_layout.addWidget(self.status)
        composer = QHBoxLayout()
        self.message = MessageInput()
        self.message.submitted.connect(self._send)
        composer.addWidget(self.message, 1)
        self.send = QPushButton("Send")
        self.send.setObjectName("primary")
        self.send.clicked.connect(self._send)
        composer.addWidget(self.send)
        conversation_layout.addLayout(composer)
        self.input_hint = label(
            "Up to 4,096 characters · English / ไทย · no transcript storage", "muted"
        )
        conversation_layout.addWidget(self.input_hint)
        splitter.addWidget(middle)

        right = QWidget()
        right.setMinimumWidth(220)
        details_layout = QVBoxLayout(right)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(label("Task details", "heading"))
        self.task_view = TaskView()
        details_layout.addWidget(self.task_view, 1)
        self.demo = QPushButton("Run safe task")
        self.protected = QPushButton("Review protected demo")
        self.slow = QPushButton("Run slow task")
        self.demo.clicked.connect(lambda: self.controller.start("task"))
        self.protected.clicked.connect(lambda: self.controller.start("approval"))
        self.slow.clicked.connect(lambda: self.controller.start("slow"))
        for button in (self.demo, self.protected, self.slow):
            details_layout.addWidget(button)
        splitter.addWidget(right)
        splitter.setSizes([170, 640, 310])
        for i in range(3):
            splitter.setCollapsible(i, False)

        footer = QHBoxLayout()
        footer.addWidget(
            label("Synthetic demos only · no files, browsers or paid APIs", "muted"), 1
        )
        self.cancel = QPushButton("Cancel task")
        self.stop = QPushButton("Emergency Stop")
        self.stop.setObjectName("emergency")
        self.stop.setAccessibleName(
            "Emergency Stop; cancel run and deny pending approvals"
        )
        self.cancel.clicked.connect(self.controller.stop)
        self.stop.clicked.connect(self.controller.stop)
        footer.addWidget(self.cancel)
        footer.addWidget(self.stop)
        outer.addLayout(footer)
        self.controller.busy_changed.connect(self._busy)
        self.controller.progress.connect(self._progress)
        self.controller.completed.connect(self._completed)
        self.controller.error.connect(self._error)
        self.controller.stopping.connect(self._stopping)
        self.controller.approval_requested.connect(self._approval)
        self.controller.approval_closed.connect(self._dismiss_approval)
        self.controller.preferences_changed.connect(self._preferences)
        self.controller.idle.connect(self._idle)
        self._preferences(self.controller.preferences)
        self._busy(False)
        self._welcome()
        QWidget.setTabOrder(self.message, self.send)
        QWidget.setTabOrder(self.send, self.stop)
        QWidget.setTabOrder(self.stop, self.settings_button)
        self.message.setFocus()

    def _welcome(self) -> None:
        self.conversation.add(
            "Sofia",
            "สวัสดี! Hi, I'm Sofia. Say hello, try a safe task, "
            "or review a protected demo. Everything here runs offline. "
            "Your approval stays yours.",
            self.controller.preferences,
        )

    def _new_chat(self) -> None:
        if self.controller.busy:
            return
        replacement = Conversation()
        parent = self.conversation.parentWidget()
        assert parent is not None
        layout = parent.layout()
        assert layout is not None
        layout.replaceWidget(self.conversation, replacement)
        self.conversation.deleteLater()
        self.conversation = replacement
        self.message.clear()
        self._welcome()

    def _send(self) -> None:
        text = self.message.toPlainText()
        if self.controller.start("chat", text):
            self.conversation.add("You", text, self.controller.preferences)
            self.message.clear()

    @Slot(bool)
    def _busy(self, busy: bool) -> None:
        for widget in (
            self.message,
            self.send,
            self.demo,
            self.protected,
            self.slow,
            self.new_chat,
        ):
            widget.setEnabled(not busy and not self._closing)
        self.cancel.setEnabled(busy)
        self.stop.setEnabled(busy)
        if busy:
            self.status.setText("Processing offline…")
        elif not self._closing:
            self.message.setFocus()

    @Slot(object)
    def _progress(self, snapshot: Progress) -> None:
        self.status.setText(f"Task · {snapshot.status.value}")
        self.task_view.progress(snapshot)

    @Slot()
    def _stopping(self) -> None:
        self.status.setText("Stopping… waiting for the worker to observe cancellation")
        self.cancel.setEnabled(False)

    @Slot(object)
    def _completed(self, outcome: Outcome) -> None:
        self.task_view.outcome(outcome)
        if outcome.cancelled:
            text = (
                "Cancelled. Earlier effects may already have committed."
                if outcome.result is None
                or outcome.result.status is TaskStatus.CANCELLED
                else "Finished after Stop was requested. Review the actual result; effects may have committed."
            )
        elif outcome.error:
            text = (
                "I couldn't finish this request (worker_failed). You can try a new run."
            )
        elif outcome.result:
            text = f"Task {outcome.result.status.value}."
            if outcome.result.failure:
                text += f" Reason: {outcome.result.failure.value}."
        else:
            text = outcome.reply
        self.status.setText(
            text
            if outcome.result or outcome.cancelled or outcome.error
            else "Idle · Reply ready"
        )
        self.conversation.add("Sofia", text, self.controller.preferences)
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for item in reversed(tuple(self.controller.history)[-MAX_TASKS:]):
            state = (
                "stop requested"
                if item.cancelled
                else item.result.status.value
                if item.result
                else "failed"
                if item.error
                else "offline reply"
            )
            self.tasks.addItem(f"{state}\n{item.task_id[:8]}")
        self.tasks.blockSignals(False)

    def _show_history(self, index: int) -> None:
        if not self.controller.busy and 0 <= index < len(self.controller.history):
            self.task_view.outcome(tuple(reversed(self.controller.history))[index])

    @Slot(str)
    def _error(self, code: str) -> None:
        self.status.setText(
            "Enter a message of 1–4,096 characters."
            if code == "invalid_message"
            else "This request is unavailable."
        )

    @Slot(object)
    def _approval(self, review: Review) -> None:
        if self.approval_dialog is not None or self._closing:
            self.controller.decide(review, False)
            return
        dialog = ApprovalDialog(review, self)
        self.approval_dialog = dialog
        dialog.decision.connect(self.controller.decide)
        dialog.finished.connect(self._dismiss_approval)
        dialog.show()
        self.status.setText(
            "Waiting for your decision · Emergency Stop remains available"
        )

    @Slot()
    def _dismiss_approval(self) -> None:
        dialog = self.approval_dialog
        if dialog is not None:
            self.approval_dialog = None
            if dialog.isVisible():
                dialog.reject()
            dialog.deleteLater()

    def _settings(self) -> None:
        if self.settings_dialog:
            self.settings_dialog.show()
            self.settings_dialog.raise_()
            return
        dialog = SettingsDialog(self.controller.preferences, self)
        self.settings_dialog = dialog
        dialog.saved.connect(self.controller.set_preferences)
        dialog.show()

    @Slot(object)
    def _preferences(self, preferences: Preferences) -> None:
        font = self.font()
        font.setPointSize(preferences.font_size)
        self.setFont(font)
        self.setStyleSheet(stylesheet(preferences.theme))
        self.conversation.apply_preferences(preferences)

    @Slot()
    def _idle(self) -> None:
        if self._closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.settings_button.setEnabled(False)
        self._dismiss_approval()
        if self.settings_dialog:
            self.settings_dialog.reject()
        if self.controller.busy:
            event.ignore()
            self.controller.shutdown()
        else:
            self.controller.shutdown()
            event.accept()
