from dataclasses import FrozenInstanceError

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from sofia.persona import mock_reply
from sofia.ui.models import MAX_MESSAGE, MAX_MESSAGES, MAX_TASKS, Preferences


def messages(window):
    return [
        item.text()
        for item in window.conversation.findChildren(QLabel)
        if item.accessibleName().endswith(" message")
    ]


def test_startup_offline_idle(window):
    assert not window.controller.busy
    assert window.mode.text() == "● Offline Mock"
    assert "Idle" in window.task_view.toPlainText()
    assert not window.controller.history
    assert len(messages(window)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Hello",
        "สวัสดีค่ะ",
        "ไทย English 😊\nnext line",
        '<img src="file:///private.png"><a href="https://evil.test">click</a>',
        "ไทย" * 1300,
    ],
)
def test_chat_unicode_and_inert_text(window, wait, text):
    window.message.setPlainText(text)
    QTest.mouseClick(window.send, Qt.MouseButton.LeftButton)
    # A second click/Enter cannot create another request while the worker is active.
    assert not window.controller.start("chat", text)
    wait(lambda: not window.controller.busy)
    assert messages(window)[1] == text
    assert messages(window)[2] == mock_reply()
    assert len(window.controller.history) == 1
    for item in window.conversation.findChildren(QLabel):
        assert item.textFormat() == Qt.TextFormat.PlainText
        assert not item.openExternalLinks()
    assert window.message.toPlainText() == ""


def test_enter_shift_enter_empty_and_bounds(window, wait):
    for text in ("", " \n\t"):
        window.message.setPlainText(text)
        QTest.keyClick(window.message, Qt.Key.Key_Return)
        assert not window.controller.busy
    window.message.setPlainText("English ไทย")
    QTest.keyClick(window.message, Qt.Key.Key_End)
    QTest.keyClick(window.message, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert window.message.toPlainText() == "English ไทย\n"
    QTest.keyClick(window.message, Qt.Key.Key_Return)
    wait(lambda: not window.controller.busy)
    assert len(window.controller.history) == 1
    window.message.setPlainText("ก" * (MAX_MESSAGE + 100))
    assert len(window.message.toPlainText()) == MAX_MESSAGE
    assert not window.controller.start("chat", "x" * (MAX_MESSAGE + 1))


def test_bounded_conversation_and_new_chat(window):
    for _ in range(MAX_MESSAGES + 5):
        window.conversation.add("You", "ไทย\n" * 50, window.controller.preferences)
    assert len(messages(window)) == MAX_MESSAGES
    window.new_chat.click()
    assert len(messages(window)) == 1
    window.resize(840, 600)
    assert window.stop.isVisible()


@pytest.mark.parametrize("theme", ["Dark", "Light", "System"])
def test_settings_apply_during_task_no_persistence(
    window, wait, theme, tmp_path, monkeypatch
):
    from PySide6.QtCore import QSettings

    monkeypatch.setattr(
        QSettings, "setValue", lambda *args: pytest.fail("Unexpected persistence")
    )
    window.slow.click()
    window.settings_button.click()
    dialog = window.settings_dialog
    assert dialog.isVisible()
    dialog.theme.setCurrentText(theme)
    dialog.font_size.setValue(14)
    dialog.timestamps.setChecked(False)
    dialog._save()
    assert window.controller.preferences == Preferences(theme, 14, False)
    assert window.font().pointSize() == 14
    assert all(not item.isVisible() for item in window.conversation._timestamps)
    window.stop.click()
    wait(lambda: not window.controller.busy)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "values",
    [
        dict(theme="untrusted"),
        dict(font_size=9),
        dict(font_size=19),
        dict(font_size=True),
        dict(timestamps="yes"),
    ],
)
def test_invalid_preferences(values):
    with pytest.raises(ValueError, match="invalid_preferences"):
        Preferences(**values)


def test_snapshots_immutable_and_history_bounded(window):
    from sofia.ui.models import Outcome

    for i in range(MAX_TASKS + 5):
        window.controller.history.append(Outcome(str(i)))
    assert len(window.controller.history) == MAX_TASKS
    with pytest.raises(FrozenInstanceError):
        window.controller.preferences.theme = "Light"
