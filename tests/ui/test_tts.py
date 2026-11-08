from dataclasses import replace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPushButton
from tts_helpers import CAPS, FakeEngine

from sofia.tts.models import TTSConfig
from sofia.ui.tts_controller import TTSController


@pytest.fixture
def output(qapp):
    engines = []

    def factory(name):
        assert name == "fake"
        engine = FakeEngine()
        engines.append(engine)
        return engine

    controller = TTSController(factory=factory)
    controller.catalog = {"fake": CAPS}
    yield controller, engines
    for engine in engines:
        engine.delayed_stop = False
    controller.shutdown()
    controller.deleteLater()
    qapp.processEvents()


def enable(controller):
    assert controller.configure(TTSConfig(True, "fake", "en-US", "en"))


def test_default_no_engine_and_explicit_request(output, wait):
    tts, engines = output
    assert not tts.config.enabled and not engines
    assert not tts.speak("Hello") and tts.state == "disabled"
    enable(tts)
    assert tts.speak("Hello")
    assert tts.state == "queued" and not engines
    wait(lambda: len(engines) == 1)
    assert tts.state == "preparing"
    engines[0].emit("playing")
    assert tts.state == "playing"
    engines[0].emit("completed")
    assert tts.state == "completed" and not tts.busy and engines[0].closed


def test_fifo_error_advances_no_overlap_or_duplicate_dispatch(output, wait, qapp):
    tts, engines = output
    enable(tts)
    for text in ("one", "two", "three"):
        assert tts.speak(text)
    wait(lambda: len(engines) == 1)
    qapp.processEvents()
    assert len(engines) == 1
    engines[0].emit("playback_failed")
    wait(lambda: len(engines) == 2)
    engines[1].emit("completed")
    wait(lambda: len(engines) == 3)
    assert [e.request.text for e in engines] == ["one", "two", "three"]
    assert engines[0].closed and engines[1].closed


@pytest.mark.parametrize("phase", ["queued", "preparing", "synthesizing", "playing"])
def test_cancel_all_and_late_events(output, wait, qapp, phase):
    tts, engines = output
    enable(tts)
    tts.speak("one")
    tts.speak("two")
    if phase != "queued":
        wait(lambda: bool(engines))
        engines[0].emit(phase)
    tts.cancel()
    qapp.processEvents()
    assert not tts.busy and tts.queue.pending == 0
    count = len(engines)
    assert tts.speak("new")
    wait(lambda: len(engines) == count + 1)
    if count:
        engines[0].emit("completed")
        engines[0].emit("playing")
    assert tts.state == "preparing" and tts.busy


def test_stop_acknowledgement_blocks_new_sessions_and_shutdown(output, wait):
    tts, engines = output
    enable(tts)
    tts.speak("Hello")
    wait(lambda: bool(engines))
    old = engines[0]
    old.delayed_stop = True
    tts.cancel()
    assert tts.busy and tts.state == "stopping" and not old.closed
    assert not tts.speak("cannot start")
    old.emit("playing")
    tts._stop_begin -= 6
    tts._tick()
    assert tts.busy and tts.state == "stop_timeout"
    tts.shutdown()
    old.emit("cancelled")
    assert old.closed and not tts.busy
    assert not tts.speak("after close")


def test_timeout_and_responsive_event_loop(output, wait):
    tts, engines = output
    enable(tts)
    tts.speak("Hello")
    wait(lambda: bool(engines))
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    wait(lambda: ticks)
    tts._begin -= 121
    tts._tick()
    assert tts.state == "timeout" and not tts.busy


def test_rejected_request_preserves_active_playback_status(output, wait):
    tts, engines = output
    enable(tts)
    tts.speak("one")
    wait(lambda: bool(engines))
    engines[0].emit("playing")
    assert not tts.speak("<speak>untrusted</speak>")
    assert tts.state == "playing" and tts.notice == "invalid_text"


def test_clear_stop_current_replace_and_config_snapshot(output, wait):
    tts, engines = output
    enable(tts)
    tts.speak("one")
    wait(lambda: bool(engines))
    tts.speak("discarded")
    tts.speak("replacement", replace_pending=True)
    tts.configure(replace(tts.config, volume=0.2))
    tts.stop_current()
    wait(lambda: len(engines) == 2)
    assert engines[1].request.text == "replacement"
    assert engines[1].request.config.volume == 0.7
    tts.speak("pending")
    tts.clear_pending()
    assert tts.busy and not tts.queue.pending
    tts.configure(replace(tts.config, enabled=False))
    assert not tts.busy


def setup_window(window, output, monkeypatch):
    tts, engines = output
    window.tts.factory = tts.factory
    window.tts.catalog = tts.catalog
    monkeypatch.setattr(window.tts, "discover", lambda: None)
    enable(window.tts)
    return window.tts, engines


def test_ui_read_only_visible_response_and_stop(window, output, monkeypatch, wait):
    tts, engines = setup_window(window, output, monkeypatch)
    window.conversation.add("You", "Private user draft", window.controller.preferences)
    window.conversation.add("Sofia", "Hello", window.controller.preferences)
    buttons = [
        b
        for b in window.conversation.findChildren(QPushButton)
        if b.text() == "Read Aloud"
    ]
    assert len(buttons) == 2  # Welcome + assistant, never user text.
    buttons[-1].click()
    wait(lambda: bool(engines))
    assert engines[0].request.text == "Hello"
    engines[0].emit("playing")
    assert "Speaking" in window.playback_status.text()
    assert window.stop.isEnabled() and window.stop_speaking.isEnabled()
    window.stop_speaking.click()
    assert not tts.busy and engines[0].stops == 1


@pytest.mark.parametrize("action", ["stop", "cancel", "task", "new", "close"])
def test_gui_cancellation_paths(window, output, monkeypatch, wait, action):
    tts, engines = setup_window(window, output, monkeypatch)
    tts.speak("Hello")
    wait(lambda: bool(engines))
    if action == "stop":
        window.stop.click()
    elif action == "cancel":
        # A run must be active for Cancel task to be enabled.
        window.controller.start("slow")
        window.cancel.click()
    elif action == "task":
        window.controller.start("task")
    elif action == "new":
        window.new_chat.click()
    else:
        window.close()
    assert not tts.busy and engines[0].closed


def test_half_duplex_waits_for_ack_and_never_auto_opens_mic(
    window, output, monkeypatch, wait
):
    tts, engines = setup_window(window, output, monkeypatch)
    tts.speak("Hello")
    wait(lambda: bool(engines))
    engines[0].delayed_stop = True
    window._record()
    assert window.speech.capture.starts == 0 and tts.state == "stopping"
    engines[0].emit("cancelled")
    assert window.speech.capture.starts == 0
    window._record()
    assert window.speech.capture.starts == 1
    assert not tts.speak("No feedback")
    window.speech.cancel()


def test_settings_voice_enumeration_and_honest_gender(window, output, monkeypatch):
    tts, engines = setup_window(window, output, monkeypatch)
    window.settings_button.click()
    dialog = window.settings_dialog
    assert not dialog.voice_enabled.isChecked()
    dialog.voice_enabled.setChecked(True)
    assert "Female" in dialog.voice.currentText()
    dialog.voice_language.setCurrentIndex(dialog.voice_language.findData("th-TH"))
    assert (
        dialog.voice.currentData() == "th" and "No female" in dialog.voice_hint.text()
    )
    dialog.voice_rate.setValue(-20)
    dialog.voice_volume.setValue(25)
    dialog._save()
    assert window.controller.preferences.tts == TTSConfig(
        True, "fake", "th-TH", "th", -0.2, 0.25
    )
    assert tts.config == window.controller.preferences.tts
    assert not engines


def test_settings_test_voice_unsaved_preview(window, output, monkeypatch, wait):
    tts, engines = setup_window(window, output, monkeypatch)
    previous = tts.config
    window.settings_button.click()
    dialog = window.settings_dialog
    dialog.voice_enabled.setChecked(True)
    dialog.voice_test.click()
    wait(lambda: bool(engines))
    assert engines[0].request.text == "Hello! I'm Sofia. How can I help you today?"
    assert tts.config == previous
    dialog.reject()


def test_approval_blocks_output_and_cannot_be_decided_by_speech(
    window, output, monkeypatch, wait
):
    tts, engines = setup_window(window, output, monkeypatch)
    window.protected.click()
    wait(lambda: window.approval_dialog is not None)
    assert not tts.speak("Approve this action")
    assert window.approval_dialog is not None and not engines
    window.stop.click()
    wait(lambda: not window.controller.busy)


def test_privacy_sanitized_engine_failure_and_no_files(
    output, monkeypatch, caplog, tmp_path, wait
):
    from sofia.security import PermissionPolicy
    from sofia.tools import ToolRegistry

    tts, engines = output
    enable(tts)
    private = "PRIVATE synthetic secret"

    def failure(*args):
        raise RuntimeError(private)

    monkeypatch.setattr(PermissionPolicy, "approve", failure)
    monkeypatch.setattr(ToolRegistry, "execute", failure)
    monkeypatch.chdir(tmp_path)
    caplog.set_level("INFO", logger="sofia.tts")
    tts.speak(private)
    wait(lambda: bool(engines))
    engines[0].emit("completed")
    tts.factory = failure
    tts.speak(private)
    wait(lambda: tts.state == "playback_failed")
    assert private not in caplog.text
    assert "tts_requested" in caplog.text and "tts_completed" in caplog.text
    assert list(tmp_path.iterdir()) == []
