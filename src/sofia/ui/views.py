"""Widgets render inert text only; task views consume sanitized core records."""

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sofia.security import BudgetSnapshot
from sofia.ui.models import MAX_MESSAGE, MAX_MESSAGES, Outcome, Preferences, Progress


def label(text: str, name: str = "") -> QLabel:
    widget = QLabel()
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setText(text)
    widget.setWordWrap(True)
    widget.setObjectName(name)
    widget.setOpenExternalLinks(False)
    return widget


class MessageInput(QPlainTextEdit):
    submitted = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setPlaceholderText(
            "Message Sofia…  Enter to send · Shift+Enter for a new line"
        )
        self.setAccessibleName("Message Sofia")
        self.setMaximumHeight(130)
        self.textChanged.connect(self._bound)

    def _bound(self) -> None:
        text = self.toPlainText()
        if len(text) > MAX_MESSAGE:
            cursor = self.textCursor()
            position = min(cursor.position(), MAX_MESSAGE)
            self.setPlainText(text[:MAX_MESSAGE])
            cursor = self.textCursor()
            cursor.setPosition(position)
            self.setTextCursor(cursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class Conversation(QScrollArea):
    read_aloud = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName("Conversation; ephemeral offline messages")
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(16, 16, 16, 16)
        self.rows.setSpacing(12)
        self.rows.addStretch()
        self.setWidget(self.body)
        self._timestamps: list[QLabel] = []
        self.verticalScrollBar().rangeChanged.connect(self._scroll)

    def add(self, role: str, text: str, preferences: Preferences) -> None:
        frame = QFrame()
        frame.setObjectName("user" if role == "You" else "assistant")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        header = label(f"{role} · {datetime.now().strftime('%H:%M')}", "muted")
        header.setVisible(preferences.timestamps)
        self._timestamps.append(header)
        layout.addWidget(header)
        content = label(text[:MAX_MESSAGE])
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.setAccessibleName(f"{role} message")
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(content)
        if role == "Sofia":
            read = QPushButton("Read Aloud")
            read.setAccessibleName("Read this visible Sofia response aloud")
            read.clicked.connect(lambda: self.read_aloud.emit(content.text()))
            layout.addWidget(read)
        self.rows.insertWidget(self.rows.count() - 1, frame)
        while len(self._timestamps) > MAX_MESSAGES:
            self._timestamps.pop(0)
            item = self.rows.takeAt(0)
            assert item is not None
            old_frame = item.widget()
            assert old_frame is not None
            old_frame.setParent(None)
            old_frame.deleteLater()

    def _scroll(self, minimum: int, maximum: int) -> None:
        self.verticalScrollBar().setValue(maximum)

    def apply_preferences(self, preferences: Preferences) -> None:
        for timestamp in self._timestamps:
            timestamp.setVisible(preferences.timestamps)


class TaskView(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setAccessibleName("Task details and sanitized history")
        self.setPlainText(
            "Idle\n\nYour tasks and approvals will appear here.\n"
            "Offline only · 8 steps · 0 tokens · $0 limit"
        )

    def progress(self, snapshot: Progress) -> None:
        self.setPlainText(
            f"Task {snapshot.task_id}\nStatus: {snapshot.status.value}\n\n"
            "Live status is shown only at trusted callback checkpoints.\n"
            "Final steps and transitions appear when execution returns.\n\n"
            + self._budget(snapshot.budget)
        )

    @staticmethod
    def _budget(budget: BudgetSnapshot) -> str:
        return (
            f"Steps: {budget.steps} used / {budget.remaining_steps} remaining\n"
            f"Tokens: {budget.committed_tokens} committed / {budget.reserved_tokens} reserved\n"
            f"USD: {budget.committed_usd} committed / {budget.reserved_usd} reserved\n"
            f"Accounting blocked: {budget.blocked}"
        )

    def outcome(self, outcome: Outcome) -> None:
        result = outcome.result
        if result is None:
            self.setPlainText(
                "Cancelled"
                if outcome.cancelled
                else "Failed: worker_failed"
                if outcome.error
                else "Offline reply complete"
            )
            return
        lines = [
            f"Task {result.task_id}",
            f"Status: {result.status.value}",
            f"Completed steps: {result.completed_steps} · Attempts: {result.attempts}",
            f"Failure: {result.failure.value if result.failure else 'none'}",
            self._budget(result.budget),
            "",
            "Actual transitions:",
            " → ".join(state.value for state in result.transitions),
            "",
            "Step history:",
        ]
        for record in result.history:
            lines.append(
                f"{record.step_id}. {record.tool} · attempt {record.attempt} · "
                f"{record.execution.value} · verification {record.verification.value}"
                f" · {record.failure.value if record.failure else 'no failure'}"
            )
        if outcome.cancelled:
            lines.append(
                "Stop was requested. Earlier effects may already have committed."
            )
        self.setPlainText("\n".join(lines))
