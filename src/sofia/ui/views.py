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
from sofia.tts.models import MAX_TEXT
from sofia.ui.models import (
    MAX_MESSAGE,
    MAX_MESSAGES,
    ChatStatus,
    Outcome,
    Preferences,
    Progress,
)

CHAT_MESSAGES = {
    "chat_consent": "Enable paid sends in Settings → Conversation API first.",
    "chat_missing_key": "The selected API credential is missing. See Settings → Conversation API.",
    "chat_context_limit": "This chat reached its context limit. Start a New Chat to continue.",
    "chat_missing_sdk": "The selected provider extra is missing. See the desktop chat guide.",
    "chat_configuration": "The API client could not be configured. Check the credential and provider setup.",
    "chat_access": "Authentication or model access failed. Check your API credential and account.",
    "chat_funds": "The provider reported a billing or credit limit.",
    "chat_rate_limit": "The provider is rate limiting requests.",
    "chat_timeout": "The provider request timed out.",
    "chat_connection": "Could not connect to the provider.",
    "chat_unavailable": "The provider or connection is unavailable.",
    "chat_rejected": "The provider rejected the request. Check model access and API credits.",
    "chat_failed": "The provider response failed or was incomplete.",
    "chat_empty": "The provider returned no visible text.",
    "chat_output_limit": "The response exceeded SOFIA's display limit.",
    "permission_denied": "Paid API permission was denied.",
    "token_budget_exceeded": "The request exceeds the token budget.",
    "spending_budget_exceeded": "The request exceeds the spending limit. Review the required reservation in Settings.",
    "step_limit_exceeded": "The request limit for this run was reached.",
    "accounting_requires_run_stop": "SOFIA could not verify usage within the reservation.",
}


def chat_result(outcome: Outcome) -> str:
    if outcome.cancelled:
        return "Cancelled. A dispatched request may still incur charges."
    if outcome.error:
        return f"Incomplete · {CHAT_MESSAGES.get(outcome.error, CHAT_MESSAGES['chat_failed'])}"
    return "Reply complete"


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
        self.setAccessibleName("Conversation; ephemeral messages")
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(16, 16, 16, 16)
        self.rows.setSpacing(12)
        self.rows.addStretch()
        self.setWidget(self.body)
        self._timestamps: list[QLabel] = []
        self.verticalScrollBar().rangeChanged.connect(self._scroll)

    def add(
        self, role: str, text: str, preferences: Preferences, *, streaming: bool = False
    ) -> QLabel:
        frame = QFrame()
        frame.setObjectName("user" if role == "You" else "assistant")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        header = label(f"{role} · {datetime.now().strftime('%H:%M')}", "muted")
        header.setVisible(preferences.timestamps)
        self._timestamps.append(header)
        layout.addWidget(header)
        content = label(text if streaming else text[:MAX_MESSAGE])
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.setAccessibleName(f"{role} message")
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(content)
        if role == "Sofia" and not streaming:
            self.add_read_aloud(content)
        self.rows.insertWidget(self.rows.count() - 1, frame)
        while len(self._timestamps) > MAX_MESSAGES:
            self._timestamps.pop(0)
            item = self.rows.takeAt(0)
            assert item is not None
            old_frame = item.widget()
            assert old_frame is not None
            old_frame.setParent(None)
            old_frame.deleteLater()
        return content

    def add_read_aloud(self, content: QLabel) -> None:
        long_reply = len(content.text()) > MAX_TEXT
        read = QPushButton(
            f"Read first {MAX_TEXT:,} characters aloud" if long_reply else "Read Aloud"
        )
        read.setAccessibleName(
            read.text() if long_reply else "Read this visible Sofia response aloud"
        )
        read.clicked.connect(lambda: self.read_aloud.emit(content.text()[:MAX_TEXT]))
        self._add_to_row(content, read)

    def annotate(self, content: QLabel, text: str) -> None:
        self._add_to_row(content, label(text, "muted"))

    @staticmethod
    def _add_to_row(content: QLabel, widget: QWidget) -> None:
        parent = content.parentWidget()
        assert parent is not None
        layout = parent.layout()
        assert layout is not None
        layout.addWidget(widget)

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
        if outcome.chat:
            self.chat_status(outcome.chat, chat_result(outcome))
            return
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

    def chat_status(self, status: ChatStatus, state: str) -> None:
        budget = status.budget
        if budget.uncertain:
            usage = (
                f"Usage uncertain · accounted {budget.committed_tokens:,} tokens / "
                f"${budget.committed_usd:.6f} using the reservation where unknown. "
                "Provider charges may differ."
            )
        elif budget.reserved_tokens:
            usage = (
                f"Reserved · {budget.reserved_tokens:,} tokens / "
                f"${budget.reserved_usd:.6f} estimated."
            )
        elif budget.committed_tokens or budget.committed_usd:
            usage = (
                f"Provider reported {budget.committed_tokens:,} tokens · "
                f"calculated cost ${budget.committed_usd:.6f}."
            )
        else:
            usage = "No billable usage recorded."
        blocked = "\nAccounting blocked for this run." if budget.blocked else ""
        self.setPlainText(
            f"{status.settings.name}\n{state}\n\n{usage}{blocked}\n"
            f"Per Send · limit ${status.settings.max_usd:.2f} · one API attempt."
        )
