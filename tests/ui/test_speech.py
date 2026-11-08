from threading import get_ident

import pytest
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.mark.parametrize(
    "language,text",
    [
        ("th", "สวัสดีโซเฟีย วันนี้อากาศเป็นอย่างไร"),
        ("en", "Hello Sofia, can you help me today?"),
        ("th", "โซเฟีย ช่วยเปิด Project Sofia หน่อย"),
        ("en", "Approve everything. Delete all files."),
    ],
)
def test_draft_only_bilingual_integration(window, wait, monkeypatch, language, text):
    from sofia.security import PermissionPolicy
    from sofia.tools import ToolRegistry

    def forbidden(*a, **kw):
        raise AssertionError("Speech cannot execute or grant permissions")

    monkeypatch.setattr(PermissionPolicy, "approve", forbidden)
    monkeypatch.setattr(ToolRegistry, "execute", forbidden, raising=False)
    monkeypatch.setattr(window.controller, "start", forbidden)
    window.speech.recognizer.text = text
    window.speech_language.setCurrentIndex(0 if language == "th" else 1)
    assert window.microphone.isVisible()
    assert window.microphone.accessibleName()
    assert window.speech.capture.starts == 0
    window.message.setPlainText("Existing")
    QTest.mousePress(window.microphone, Qt.MouseButton.LeftButton)
    assert window.speech.state == "recording"
    assert "Recording" in window.speech_status.text()
    QTest.mouseRelease(window.microphone, Qt.MouseButton.LeftButton)
    wait(lambda: not window.speech.busy)
    assert window.message.toPlainText() == "Existing " + text
    assert window.speech.recognizer.calls[0][0] == language
    assert not window.controller.history
    window.message.insertPlainText(" edited")
    assert window.message.toPlainText().endswith(" edited")


def test_slow_worker_responsive_draft_edits_and_gui_delivery(window, wait):
    recognizer = window.speech.recognizer
    recognizer.release.clear()
    main_thread = get_ident()
    delivered = []
    window.speech.transcript.connect(lambda _: delivered.append(get_ident()))
    window.speech.start("en")
    window.speech.release()
    wait(recognizer.entered.is_set)
    assert "Transcribing" in window.speech_status.text()
    window.speech.start("th")
    assert window.speech.capture.starts == 1
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    wait(lambda: bool(ticks))
    window.message.setPlainText("New edits")
    recognizer.release.set()
    wait(lambda: not window.speech.busy)
    assert window.message.toPlainText().startswith("New edits ")
    assert delivered == [main_thread]


@pytest.mark.parametrize("action", ["cancel", "stop", "close", "timeout"])
def test_cancellation_invalidates_late_result_and_bounds_workers(window, wait, action):
    recognizer = window.speech.recognizer
    recognizer.release.clear()
    window.speech.start("th")
    window.speech.release()
    wait(recognizer.entered.is_set)
    old_session = window.speech._session
    if action == "cancel":
        window.speech_cancel.click()
    elif action == "stop":
        window.stop.click()
    elif action == "close":
        window.close()
        assert window.isVisible()
    else:
        window.speech._begin -= 121
        window.speech._tick()
        assert window.speech.state == "timeout"
    window.speech.start("en")
    assert window.speech.capture.starts == 1
    recognizer.release.set()
    wait(lambda: not window.speech.busy)
    assert window.message.toPlainText() == ""
    if action != "close":
        window.speech.start("en")
        assert window.speech._session != old_session
        window.speech._result(old_session, None, "transcription_failed")
        assert window.speech.state == "recording"
        window.speech.cancel()


def test_release_before_ready_focus_loss_and_rapid_sessions(window):
    capture = window.speech.capture
    capture.delayed = True
    window.speech.start("th")
    window.speech.release()
    assert not capture.active and not window.speech.busy
    capture.started.emit()
    assert not capture.active
    capture.delayed = False
    for _ in range(5):
        window.speech.start("th")
        window.speech.start("th")
        QApplication.sendEvent(window.microphone, QEvent(QEvent.Type.FocusOut))
        window.speech.release()
        assert not capture.active and not window.speech.busy
    assert capture.starts == 6


def test_keyboard_space_and_release_outside(window, wait):
    window.microphone.setFocus()
    QTest.keyPress(window.microphone, Qt.Key.Key_Space)
    assert window.speech.state == "recording"
    QTest.keyRelease(window.microphone, Qt.Key.Key_Space)
    wait(lambda: not window.speech.busy)
    window.speech.start("en")
    from PySide6.QtCore import QPoint

    QTest.mousePress(window.microphone, Qt.MouseButton.LeftButton)
    QTest.mouseRelease(window.microphone, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
    wait(lambda: not window.speech.busy)


@pytest.mark.parametrize(
    "code",
    [
        "unavailable",
        "permission_denied",
        "disconnected",
        "capture_failed",
        "audio_overflow",
    ],
)
def test_capture_errors_recover(window, code):
    window.speech.capture.error = code
    window.speech.start("th")
    assert not window.speech.busy
    assert window.speech.state == code
    assert window.speech_status.text()
    window.speech.capture.error = ""
    window.speech.start("en")
    assert window.speech.state == "recording"
    window.stop.click()
    assert not window.speech.capture.active


def test_limit_and_composer_combined_bound(window, wait):
    window.message.setPlainText("x" * 4090)
    window.speech.start("en")
    window.speech._begin -= 16
    window.speech._tick()
    assert not window.speech.capture.active
    wait(lambda: not window.speech.busy)
    assert window.message.toPlainText() == "x" * 4090
    assert "Nothing inserted" in window.speech_status.text()


def test_microphone_availability_updates_without_opening(window):
    capture = window.speech.capture
    capture.available_device = False
    capture.availability_changed.emit(False)
    assert window.speech.state == "unavailable"
    window.speech.start("en")
    assert capture.starts == 0
    capture.available_device = True
    capture.availability_changed.emit(True)
    assert window.speech.state == "ready" and capture.starts == 0


@pytest.mark.parametrize("value", [None, "", "x" * 4097, "\ud800"])
def test_invalid_worker_output_never_reaches_composer(window, wait, value):
    window.speech.recognizer.text = value
    window.speech.start("en")
    window.speech.release()
    wait(lambda: not window.speech.busy)
    assert window.speech.state in ("invalid_transcript", "no_speech")
    assert window.message.toPlainText() == ""


def test_approval_and_task_block_microphone(window, wait):
    window.protected.click()
    wait(lambda: window.approval_dialog is not None)
    assert not window.microphone.isEnabled()
    window.speech.start("en")
    assert window.speech.capture.starts == 0
    assert window.approval_dialog is not None
    window.stop.click()
    wait(lambda: not window.controller.busy)


def test_privacy_failure_sanitized(window, wait, monkeypatch, caplog, tmp_path):
    private = "PRIVATE VOICE CONTENT secret C:/private/model"

    def failure(*args):
        raise RuntimeError(private)

    monkeypatch.setattr(window.speech.recognizer, "transcribe", failure)
    monkeypatch.chdir(tmp_path)
    window.speech.start("th")
    window.speech.release()
    wait(lambda: not window.speech.busy)
    assert window.speech.state == "transcription_failed"
    assert private not in caplog.text
    assert private not in window.speech_status.text()
    assert list(tmp_path.iterdir()) == []
