"""Offscreen Qt only for this test session; no desktop interaction required."""

from time import sleep

import pytest

pytest.importorskip("PySide6", reason="Install the optional ui extra for Qt tests")

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent
from PySide6.QtWidgets import QApplication


def wait_until(predicate, timeout=4000):
    clock = QElapsedTimer()
    clock.start()
    while not predicate() and clock.elapsed() < timeout:
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        # Let Python workers run between event-loop turns, including during native I/O.
        sleep(0.005)
    assert predicate(), "Qt condition did not complete before timeout"


@pytest.fixture(scope="session")
def qapp():
    patch = pytest.MonkeyPatch()
    patch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.processEvents()
    patch.undo()


@pytest.fixture
def window(qapp, monkeypatch):
    import os
    import subprocess
    import webbrowser

    from speech_helpers import FakeCapture, FakeRecognizer

    from sofia.tts import native
    from sofia.ui import speech_controller
    from sofia.ui.main_window import MainWindow

    monkeypatch.setattr(speech_controller, "AudioCapture", FakeCapture)
    # Ordinary UI tests enumerate no native voices and never touch speakers.
    monkeypatch.setattr(native, "available_engines", lambda: ())
    monkeypatch.setattr(
        speech_controller, "OfflineRecognizer", lambda path: FakeRecognizer()
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected process, URL, or desktop action")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", forbidden)
    widget = MainWindow()
    widget.show()
    yield widget
    widget.close()
    wait_until(
        lambda: (
            not widget.controller.busy
            and not widget.speech.busy
            and widget.memory_dialog is None
        )
    )
    qapp.processEvents()
    widget.deleteLater()


@pytest.fixture
def wait():
    return wait_until
