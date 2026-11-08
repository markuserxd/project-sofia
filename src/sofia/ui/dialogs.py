"""Nonblocking, plain-text review and session-only settings dialogs."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sofia.ui.approval import Review
from sofia.ui.models import Preferences


class ApprovalDialog(QDialog):
    decision = Signal(object, bool)

    def __init__(self, review: Review, parent: QWidget) -> None:
        super().__init__(parent)
        self.review = review
        self._answered = False
        self.setWindowTitle("SOFIA · Review protected action")
        self.setModal(False)  # Emergency Stop stays accessible in the main window.
        self.resize(560, 480)
        layout = QVBoxLayout(self)
        heading = QLabel("Your decision is needed")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Exact protected action details")
        self.details.setPlainText(
            f"Adapter: Harmless in-memory fixture\n"
            f"Run: {review.task_id}\nInstance: {review.adapter_id}\n"
            f"Preview: {review.preview_id}\nAction: {review.action.value}\n"
            f"Target: {review.target}\nOperation: Replace one empty synthetic note\n\n"
            f"Exact new content:\n{review.payload}\n\n"
            "Consequence: Changes one disposable memory cell; no file is written.\n"
            "Reversible: Discarded when this run ends.\n"
            "Warning: Only this exact proposal is authorized, once. "
            "Expires after 60 seconds. Closing this window means Deny."
        )
        layout.addWidget(self.details)
        self.deny = QPushButton("Deny")
        self.approve = QPushButton("Approve this action")
        self.deny.setAccessibleName("Deny protected action")
        self.approve.setAccessibleName("Approve this exact action once")
        self.deny.setDefault(True)
        self.approve.setAutoDefault(False)
        layout.addWidget(self.deny)
        layout.addWidget(self.approve)
        self.deny.clicked.connect(self.reject)
        self.approve.clicked.connect(self._approve)
        QTimer.singleShot(60_000, self, self.reject)
        self.deny.setFocus()

    def _answer(self, approved: bool) -> None:
        if not self._answered:
            self._answered = True
            self.decision.emit(self.review, approved)

    def _approve(self) -> None:
        self._answer(True)
        self.accept()

    def reject(self) -> None:
        self._answer(False)
        super().reject()


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(self, preferences: Preferences, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("SOFIA · Settings")
        layout = QFormLayout(self)
        self.theme = QComboBox()
        self.theme.addItems(["Dark", "Light", "System"])
        self.theme.setCurrentText(preferences.theme)
        self.theme.setAccessibleName("Theme")
        self.font_size = QSpinBox()
        self.font_size.setRange(10, 18)
        self.font_size.setValue(preferences.font_size)
        self.font_size.setAccessibleName("Font size in points")
        self.timestamps = QCheckBox("Show message timestamps")
        self.timestamps.setChecked(preferences.timestamps)
        layout.addRow("Theme", self.theme)
        layout.addRow("Font size", self.font_size)
        layout.addRow(self.timestamps)
        layout.addRow(
            QLabel(
                "Provider: Offline Mock\nOpenAI / Anthropic: unavailable in this GUI"
            )
        )
        layout.addRow(
            QLabel(
                "Preferences last for this session. No credentials or transcripts are stored."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _save(self) -> None:
        self.saved.emit(
            Preferences(
                self.theme.currentText(),
                self.font_size.value(),
                self.timestamps.isChecked(),
            )
        )
        self.accept()
