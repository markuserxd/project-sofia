"""A small desktop shell; widgets cannot register tools or grant capabilities."""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sofia.core import TaskStatus
from sofia.persona import PersonaConfig, mock_reply
from sofia.storage import MemoryManager
from sofia.ui.approval import Review
from sofia.ui.controller import Controller
from sofia.ui.dialogs import ApprovalDialog, SettingsDialog
from sofia.ui.memory_dialog import MemoryDialog
from sofia.ui.models import MAX_TASKS, ChatStatus, Outcome, Preferences, Progress
from sofia.ui.speech_controller import MESSAGES, SpeechController
from sofia.ui.themes import stylesheet
from sofia.ui.tts_controller import MESSAGES as TTS_MESSAGES
from sofia.ui.tts_controller import TTSController
from sofia.ui.views import (
    CHAT_MESSAGES,
    Conversation,
    MessageInput,
    TaskView,
    chat_result,
    label,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.controller = Controller(self)
        self.speech = SpeechController(self)
        self.tts = TTSController(self)
        self.approval_dialog: ApprovalDialog | None = None
        self.settings_dialog: SettingsDialog | None = None
        self.memory_dialog: MemoryDialog | None = None
        self._closing = False
        self._chat_route = self.controller.preferences.chat.route
        self._stream_label: QLabel | None = None
        self.setWindowTitle("SOFIA · Your desktop assistant")
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
        self.memory_button = QPushButton("Memory")
        self.memory_button.clicked.connect(self._memory)
        header.addWidget(self.memory_button)
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
        side.addWidget(
            label("Private by default\nSave notes explicitly in Memory.", "muted")
        )
        splitter.addWidget(sidebar)

        middle = QWidget()
        conversation_layout = QVBoxLayout(middle)
        conversation_layout.setContentsMargins(8, 0, 8, 0)
        self.conversation = Conversation()
        self.conversation.read_aloud.connect(self.tts.speak)
        conversation_layout.addWidget(self.conversation, 1)
        self.status = label("Idle · Ready when you are", "muted")
        self.status.setAccessibleName("Current execution status")
        conversation_layout.addWidget(self.status)
        composer = QHBoxLayout()
        self.message = MessageInput()
        self.message.submitted.connect(self._send)
        composer.addWidget(self.message, 1)
        self.microphone = QPushButton("Hold Mic")
        self.microphone.setAccessibleName("Push to talk; hold mouse or Space to record")
        self.microphone.setToolTip(
            "Hold to record locally; release to transcribe. Escape cancels."
        )
        self.microphone.setAutoRepeat(False)
        self.microphone.pressed.connect(self._record)
        self.microphone.released.connect(self.speech.release)
        self.microphone.installEventFilter(self)
        composer.addWidget(self.microphone)
        self.send = QPushButton("Send")
        self.send.setObjectName("primary")
        self.send.clicked.connect(self._send)
        composer.addWidget(self.send)
        conversation_layout.addLayout(composer)
        speech_controls = QHBoxLayout()
        self.speech_language = QComboBox()
        self.speech_language.addItem("ไทย · Thai", "th")
        self.speech_language.addItem("English", "en")
        self.speech_language.setAccessibleName("Speech transcription language")
        speech_controls.addWidget(self.speech_language)
        self.speech_status = label("", "muted")
        self.speech_status.setAccessibleName("Microphone and transcription status")
        speech_controls.addWidget(self.speech_status, 1)
        self.speech_cancel = QPushButton("Cancel voice")
        self.speech_cancel.clicked.connect(self.speech.cancel)
        speech_controls.addWidget(self.speech_cancel)
        conversation_layout.addLayout(speech_controls)
        playback_controls = QHBoxLayout()
        self.playback_status = label(TTS_MESSAGES["idle"], "muted")
        self.playback_status.setAccessibleName(
            "Speech playback status and pending count"
        )
        playback_controls.addWidget(self.playback_status, 1)
        self.clear_speech = QPushButton("Clear pending speech")
        self.clear_speech.clicked.connect(self.tts.clear_pending)
        playback_controls.addWidget(self.clear_speech)
        self.stop_speaking = QPushButton("Stop Speaking")
        self.stop_speaking.clicked.connect(self.tts.cancel)
        playback_controls.addWidget(self.stop_speaking)
        conversation_layout.addLayout(playback_controls)
        self.input_hint = label(
            "Up to 4,096 characters · English / ไทย · chat is never auto-saved", "muted"
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
            label("Demo tasks stay offline · chat is text only", "muted"), 1
        )
        self.cancel = QPushButton("Cancel task")
        self.stop = QPushButton("Emergency Stop")
        self.stop.setObjectName("emergency")
        self.stop.setAccessibleName(
            "Emergency Stop; cancel run and deny pending approvals"
        )
        self.cancel.clicked.connect(self.controller.stop)
        self.stop.clicked.connect(self.controller.stop)
        self.stop.clicked.connect(self.speech.cancel)
        self.stop.clicked.connect(self.tts.cancel)
        self.cancel.clicked.connect(self.tts.cancel)
        footer.addWidget(self.cancel)
        footer.addWidget(self.stop)
        outer.addLayout(footer)
        self.controller.busy_changed.connect(self._busy)
        self.controller.progress.connect(self._progress)
        self.controller.chat_progress.connect(self._chat_progress)
        self.controller.completed.connect(self._completed)
        self.controller.error.connect(self._error)
        self.controller.stopping.connect(self._stopping)
        self.controller.approval_requested.connect(self._approval)
        self.controller.approval_closed.connect(self._dismiss_approval)
        self.controller.preferences_changed.connect(self._preferences)
        self.controller.idle.connect(self._idle)
        self.speech.changed.connect(self._speech_changed)
        self.speech.transcript.connect(self._insert_transcript)
        self.speech.idle.connect(self._idle)
        self.tts.changed.connect(self._tts_changed)
        self.tts.idle.connect(self._idle)
        self._preferences(self.controller.preferences)
        self._busy(False)
        self._tts_changed()
        self._welcome()
        QWidget.setTabOrder(self.message, self.send)
        QWidget.setTabOrder(self.send, self.microphone)
        QWidget.setTabOrder(self.microphone, self.speech_language)
        QWidget.setTabOrder(self.speech_language, self.stop)
        QWidget.setTabOrder(self.stop, self.settings_button)
        self.message.setFocus()

    def _welcome(self) -> None:
        if not self.controller.preferences.chat.offline:
            return
        self.conversation.add(
            "Sofia",
            mock_reply(
                PersonaConfig(
                    language=self.controller.preferences.conversation_language
                )
            ),
            self.controller.preferences,
        )

    def _new_chat(self) -> None:
        if self.controller.busy:
            return
        self.controller.clear_chat()
        self.speech.cancel()
        self.tts.cancel()
        self._reset_conversation()
        self.message.clear()
        self._chat_idle_status(self.controller.preferences)

    def _reset_conversation(self) -> None:
        replacement = Conversation()
        replacement.read_aloud.connect(self.tts.speak)
        parent = self.conversation.parentWidget()
        assert parent is not None
        layout = parent.layout()
        assert layout is not None
        layout.replaceWidget(self.conversation, replacement)
        self.conversation.hide()
        self.conversation.deleteLater()
        self.conversation = replacement
        self._stream_label = None
        self._welcome()

    def _send(self) -> None:
        if not self.controller.preferences.chat.offline and self.speech.busy:
            self.status.setText("Wait for the speech draft to finish before sending.")
            return
        text = self.message.toPlainText()
        if self.controller.start("chat", text):
            self.conversation.add("You", text, self.controller.preferences)
            self.message.clear()

    @Slot(bool)
    def _busy(self, busy: bool) -> None:
        self.speech.block(busy or self._closing)
        self.tts.block(busy or self._closing or self.speech.busy)
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
        real_chat = self.controller.real_chat_active
        self.cancel.setText("Cancel response" if real_chat else "Cancel task")
        if self.settings_dialog:
            self.settings_dialog.chat.setEnabled(not real_chat)
        self.stop.setEnabled(
            busy or self.speech.busy or self.tts.busy or bool(self.tts.queue.pending)
        )
        if busy:
            self.status.setText(
                "Generating response…" if real_chat else "Processing offline…"
            )
            if real_chat:
                self.task_view.setPlainText(
                    f"{self.controller.preferences.chat.name}\nPreparing guarded request…"
                )
        elif not self._closing:
            self.message.setFocus()

    def _record(self) -> None:
        if (
            not self._closing
            and not self.controller.busy
            and self.approval_dialog is None
        ):
            self.tts.cancel()
            if self.tts.busy:
                self.speech_status.setText(
                    "Waiting for speaker Stop acknowledgement. Hold Mic again when speech stops."
                )
                return
            self.speech.start(self.speech_language.currentData())

    @Slot()
    def _speech_changed(self) -> None:
        busy = self.speech.busy
        self.tts.block(
            busy
            or self.controller.busy
            or self._closing
            or self.approval_dialog is not None
        )
        self.microphone.setEnabled(
            not self._closing
            and not self.controller.busy
            and (not busy or self.speech.state in ("initializing", "recording"))
        )
        self.speech_language.setEnabled(not busy and not self._closing)
        self.speech_cancel.setEnabled(busy)
        self.stop.setEnabled(
            busy
            or self.controller.busy
            or self.tts.busy
            or bool(self.tts.queue.pending)
        )
        text = MESSAGES[self.speech.state]
        if self.speech.state == "recording":
            text += f" · {self.speech.elapsed:.1f} / 15 s"
        if self.speech.state == "transcribing" and self.speech.limited:
            text = "Recording reached the time limit. " + text
        self.speech_status.setText(text)

    @Slot()
    def _tts_changed(self) -> None:
        pending = self.tts.queue.pending
        notice = f" · {TTS_MESSAGES[self.tts.notice]}" if self.tts.notice else ""
        self.playback_status.setText(
            f"{TTS_MESSAGES[self.tts.state]} · {pending} pending{notice}"
        )
        self.stop_speaking.setEnabled(self.tts.busy or pending > 0)
        self.clear_speech.setEnabled(pending > 0)
        self.stop.setEnabled(
            self.controller.busy or self.speech.busy or self.tts.busy or pending > 0
        )

    @Slot(str)
    def _insert_transcript(self, text: str) -> None:
        if self._closing or self.controller.busy or self.approval_dialog is not None:
            return
        current = self.message.toPlainText()
        addition = (" " if current and not current[-1].isspace() else "") + text
        if len(current) + len(addition) > 4096:
            self.speech.reject_draft()
            return
        # Append to the current draft, including edits made during inference.
        cursor = self.message.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(addition)
        self.message.setTextCursor(cursor)
        self.message.setFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.microphone:
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and isinstance(event, QMouseEvent)
                and event.button() == Qt.MouseButton.LeftButton
            ):
                # QAbstractButton may omit released() when released outside.
                self.speech.release()
            if event.type() == QEvent.Type.FocusOut and self.speech.state in (
                "initializing",
                "recording",
            ):
                self.speech.cancel()
            elif event.type() == QEvent.Type.KeyPress:
                if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
                    self.speech.cancel()
                    return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            if self.speech.state in ("initializing", "recording"):
                self.speech.cancel()
        super().changeEvent(event)

    @Slot(object)
    def _progress(self, snapshot: Progress) -> None:
        self.status.setText(f"Task · {snapshot.status.value}")
        self.task_view.progress(snapshot)

    @Slot(str, object)
    def _chat_progress(self, text: str, status: ChatStatus) -> None:
        content = self._stream_label
        if content is None and text:
            content = self._new_stream_label(status)
        if content is not None and content.text() != text:
            content.setText(text)
        self.task_view.chat_status(status, "Generating response…")

    def _new_stream_label(self, status: ChatStatus) -> QLabel:
        self._stream_label = self.conversation.add(
            f"Sofia · {status.settings.provider}",
            "",
            self.controller.preferences,
            streaming=True,
        )
        return self._stream_label

    @Slot()
    def _stopping(self) -> None:
        self.tts.cancel()
        self.status.setText("Stopping… waiting for the worker to observe cancellation")
        self.cancel.setEnabled(False)

    @Slot(object)
    def _completed(self, outcome: Outcome) -> None:
        self.task_view.outcome(outcome)
        if outcome.chat:
            state = chat_result(outcome)
            content = self._stream_label or self._new_stream_label(outcome.chat)
            if outcome.cancelled or outcome.error:
                if content.text():
                    self.conversation.annotate(content, state)
                else:
                    content.setText(state)
            else:
                content.setText(outcome.reply)
                self.conversation.add_read_aloud(content)
            self._stream_label = None
            self.status.setText(state)
        else:
            self._complete_offline(outcome)
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
                else "paid reply"
                if item.chat
                else "offline reply"
            )
            self.tasks.addItem(f"{state}\n{item.task_id[:8]}")
        self.tasks.blockSignals(False)

    def _complete_offline(self, outcome: Outcome) -> None:
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

    def _show_history(self, index: int) -> None:
        if not self.controller.busy and 0 <= index < len(self.controller.history):
            self.task_view.outcome(tuple(reversed(self.controller.history))[index])

    @Slot(str)
    def _error(self, code: str) -> None:
        self.status.setText(
            "Enter a message of 1–4,096 characters."
            if code == "invalid_message"
            else CHAT_MESSAGES.get(code, "This request is unavailable.")
        )

    @Slot(object)
    def _approval(self, review: Review) -> None:
        self.speech.block(True)
        self.tts.block(True)
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
        self.tts.discover()
        dialog = SettingsDialog(self.controller.preferences, self, self.tts)
        dialog.chat.setEnabled(not self.controller.real_chat_active)
        self.settings_dialog = dialog
        dialog.saved.connect(self.controller.set_preferences)
        dialog.finished.connect(self._settings_closed)
        dialog.show()

    def _memory(self) -> None:
        if self.memory_dialog is not None:
            self.memory_dialog.raise_()
            return
        dialog = MemoryDialog(MemoryManager(), self)
        self.memory_dialog = dialog
        dialog.finished.connect(self._memory_closed)
        dialog.idle.connect(self._idle)
        dialog.show()

    @Slot()
    def _memory_closed(self) -> None:
        dialog, self.memory_dialog = self.memory_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    @Slot()
    def _settings_closed(self) -> None:
        dialog, self.settings_dialog = self.settings_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    @Slot(object)
    def _preferences(self, preferences: Preferences) -> None:
        self.tts.configure(preferences.tts)
        font = self.font()
        font.setPointSize(preferences.font_size)
        self.setFont(font)
        self.setStyleSheet(stylesheet(preferences.theme))
        if self._chat_route != preferences.chat.route:
            self._chat_route = preferences.chat.route
            self._reset_conversation()
        self.mode.setText(
            f"● {preferences.chat.name}"
            + (
                " · API disabled"
                if not preferences.chat.offline and not preferences.chat.paid_enabled
                else ""
            )
        )
        if not self.controller.busy:
            self._chat_idle_status(preferences)
        self.conversation.apply_preferences(preferences)

    def _chat_idle_status(self, preferences: Preferences) -> None:
        chat = preferences.chat
        if chat.offline:
            self.status.setText("Idle · Ready when you are")
        elif chat.paid_enabled:
            self.status.setText(f"Ready · Send shares this chat with {chat.provider}")
        else:
            self.status.setText(
                "Paid sends disabled · Review Settings → Conversation API"
            )

    @Slot()
    def _idle(self) -> None:
        if (
            self._closing
            and not self.speech.busy
            and not self.controller.busy
            and not self.tts.busy
            and not (self.memory_dialog and self.memory_dialog.busy)
        ):
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.speech.shutdown()
        self.tts.shutdown()
        self.settings_button.setEnabled(False)
        self.memory_button.setEnabled(False)
        if self.memory_dialog:
            self.memory_dialog.shutdown()
        self._dismiss_approval()
        if self.settings_dialog:
            self.settings_dialog.reject()
        if (
            self.controller.busy
            or self.speech.busy
            or self.tts.busy
            or (self.memory_dialog and self.memory_dialog.busy)
        ):
            event.ignore()
            self.controller.shutdown()
        else:
            self.controller.shutdown()
            event.accept()
