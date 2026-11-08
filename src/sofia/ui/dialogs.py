"""Nonblocking, plain-text review and session-only settings dialogs."""

from PySide6.QtCore import Qt, QTimer, Signal
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

from sofia.tts.models import TTSConfig
from sofia.ui.approval import Review
from sofia.ui.models import Preferences
from sofia.ui.tts_controller import TTSController


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

    def __init__(
        self, preferences: Preferences, parent: QWidget, tts: TTSController
    ) -> None:
        super().__init__(parent)
        self.tts = tts
        self._initial_voice_config = preferences.tts
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
        self.voice_enabled = QCheckBox(
            "Enable local voice playback (manual Read Aloud only)"
        )
        self.voice_enabled.setChecked(preferences.tts.enabled)
        layout.addRow(self.voice_enabled)
        self.voice_engine = QComboBox()
        self.voice_engine.setAccessibleName("Local voice engine")
        self.voice_language = QComboBox()
        self.voice_language.setAccessibleName("Playback language")
        self.voice = QComboBox()
        self.voice.setAccessibleName("Installed playback voice")
        self.voice_rate = QSpinBox()
        self.voice_rate.setRange(-50, 50)
        self.voice_rate.setSingleStep(10)
        self.voice_rate.setValue(round(preferences.tts.rate * 100))
        self.voice_volume = QSpinBox()
        self.voice_volume.setRange(0, 100)
        self.voice_volume.setValue(round(preferences.tts.volume * 100))
        self.voice_pitch = QLabel("Pitch adjustment unavailable")
        self.voice_hint = QLabel()
        self.voice_hint.setWordWrap(True)
        self.voice_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.voice_test = QPushButton("Test selected voice (synthetic sentence)")
        self.voice_test.clicked.connect(self._test_voice)
        self.voice_stop = QPushButton("Stop Speaking")
        self.voice_stop.clicked.connect(tts.cancel)
        for caption, widget in (
            ("Voice engine", self.voice_engine),
            ("Playback language", self.voice_language),
            ("Voice", self.voice),
            ("Speaking speed change (%)", self.voice_rate),
            ("Speech volume (%)", self.voice_volume),
            ("Pitch", self.voice_pitch),
        ):
            layout.addRow(caption, widget)
        layout.addRow(self.voice_hint)
        layout.addRow(self.voice_test)
        layout.addRow(self.voice_stop)
        self.voice_engine.currentIndexChanged.connect(self._voice_languages)
        self.voice_language.currentIndexChanged.connect(self._voice_choices)
        self.voice_enabled.toggled.connect(self._voice_available)
        for engine in tts.catalog:
            self.voice_engine.addItem(engine, engine)
        index = self.voice_engine.findData(preferences.tts.engine)
        if index >= 0:
            self.voice_engine.setCurrentIndex(index)
        self._voice_languages()
        index = self.voice_language.findData(preferences.tts.language)
        if index >= 0:
            self.voice_language.setCurrentIndex(index)
        index = self.voice.findData(preferences.tts.voice)
        if index >= 0:
            self.voice.setCurrentIndex(index)
        self._voice_available()
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
        config = self._voice_config()
        if not self.tts.configure(config):
            self.voice_hint.setText(self.tts_message())
            return
        self.saved.emit(
            Preferences(
                self.theme.currentText(),
                self.font_size.value(),
                self.timestamps.isChecked(),
                config,
            )
        )
        self.accept()

    def _voice_config(self) -> TTSConfig:
        if not self.voice_enabled.isChecked() and not self._initial_voice_config.engine:
            return self._initial_voice_config
        return TTSConfig(
            enabled=self.voice_enabled.isChecked(),
            engine=self.voice_engine.currentData() or "",
            language=self.voice_language.currentData() or "en-US",
            voice=self.voice.currentData() or "",
            rate=self.voice_rate.value() / 100,
            volume=self.voice_volume.value() / 100,
        )

    def _voice_languages(self) -> None:
        self.voice_language.clear()
        caps = self.tts.catalog.get(self.voice_engine.currentData())
        if caps:
            for language in caps.languages:
                self.voice_language.addItem(
                    "ไทย · Thai (th-TH)"
                    if language == "th-TH"
                    else f"English ({language})",
                    language,
                )
            self.voice_rate.setEnabled(caps.rate_range is not None)
            low, high = caps.rate_range or (0, 0)
            self.voice_rate.setRange(round(low * 100), round(high * 100))
            self.voice_rate.setToolTip(caps.rate_description)
        self._voice_choices()

    def _voice_choices(self) -> None:
        self.voice.clear()
        caps = self.tts.catalog.get(self.voice_engine.currentData())
        voices = (
            []
            if caps is None
            else [
                v
                for v in caps.voices
                if v.language == self.voice_language.currentData()
            ]
        )
        for voice in voices:
            self.voice.addItem(voice.label, voice.id)
        preferred = next((i for i, v in enumerate(voices) if v.gender == "Female"), 0)
        self.voice.setCurrentIndex(preferred)
        self.voice_hint.setText(
            "No installed voice for this engine/language. See docs/text-to-speech.md."
            if not voices
            else "No female voice reported for this language. Available alternatives are shown."
            if not any(v.gender == "Female" for v in voices)
            else "Female preference uses engine metadata; voice quality requires listening."
        )
        if caps and "th-TH" not in caps.languages:
            self.voice_hint.setText(
                self.voice_hint.text() + " Thai is unavailable in this engine."
            )
        self._voice_available()

    def _voice_available(self) -> None:
        self.voice_test.setEnabled(
            self.voice_enabled.isChecked() and self.voice.count() > 0
        )

    def tts_message(self) -> str:
        from sofia.ui.tts_controller import MESSAGES

        return MESSAGES[self.tts.state]

    def _test_voice(self) -> None:
        # Preview uses explicit unsaved controls, then restores committed settings.
        previous = self.tts.config
        if self.tts.configure(self._voice_config()):
            self.tts.speak(
                "สวัสดีค่ะ ฉันโซเฟีย มีอะไรให้ช่วยไหมคะ"
                if self.voice_language.currentData() == "th-TH"
                else "Hello! I'm Sofia. How can I help you today?"
            )
            self.tts.config = previous
        self.voice_hint.setText(self.tts_message())
