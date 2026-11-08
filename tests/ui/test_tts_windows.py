"""Explicit opt-in native tests; completion does not prove human-audible sound."""

import os
import sys

import pytest

from sofia.tts.models import TTSConfig, TTSRequest


@pytest.mark.tts_native
def test_installed_windows_voices(qapp):
    if sys.platform != "win32" or os.environ.get("SOFIA_TTS_NATIVE_TEST") != "1":
        pytest.skip("Set SOFIA_TTS_NATIVE_TEST=1 on Windows for real voice enumeration")
    from sofia.tts.native import NativeEngine, available_engines

    engines = available_engines()
    assert engines, "No allowlisted local TTS engine installed"
    for name in engines:
        engine = NativeEngine(name)
        assert not engine.capabilities.network_required
        assert engine.capabilities.voices, "No Thai/English voices installed"
        engine.close()


@pytest.mark.tts_playback
@pytest.mark.parametrize(
    "language,text",
    [
        ("en-US", "Hello! I'm Sofia. How can I help you today?"),
        (
            "th-TH",
            "สวัสดีค่ะ ฉันโซเฟีย มีอะไรให้ช่วยไหมคะ",
        ),
    ],
)
def test_real_windows_playback(qapp, language, text):
    if sys.platform != "win32" or os.environ.get("SOFIA_TTS_PLAYBACK_TEST") != "1":
        pytest.skip("Set SOFIA_TTS_PLAYBACK_TEST=1 to explicitly enable speaker output")
    from PySide6.QtCore import QEventLoop, QTimer

    from sofia.tts.native import NativeEngine, available_engines

    chosen = None
    for name in available_engines():
        engine = NativeEngine(name)
        voices = [v for v in engine.capabilities.voices if v.language == language]
        if voices:
            chosen = (
                engine,
                next((v for v in voices if v.gender == "Female"), voices[0]),
            )
            break
        engine.close()

    if chosen is None:
        pytest.skip(f"No installed {language} voice; language acceptance is unverified")
    engine, voice = chosen
    events = []
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def event(state):
        events.append(state)
        if state in ("completed", "cancelled", "playback_failed", "unavailable"):
            loop.quit()

    try:
        engine.start(
            TTSRequest(text, TTSConfig(True, voice.engine, language, voice.id)),
            event,
        )
        deadline.start(45000)  # Allow a cold OS voice service to initialize.
        if not set(events) & {
            "completed",
            "cancelled",
            "playback_failed",
            "unavailable",
        }:
            loop.exec()
        assert "playing" in events and events[-1] == "completed"
    finally:
        deadline.stop()
        engine.stop()
        if engine._thread is not None:
            deadline.start(5000)
            loop.exec()
        engine.close()


@pytest.mark.tts_playback
@pytest.mark.parametrize("backend", ["windows-local"])
@pytest.mark.parametrize("phase", ["preparing", "playing"])
def test_real_windows_stop(qapp, backend, phase):
    if sys.platform != "win32" or os.environ.get("SOFIA_TTS_PLAYBACK_TEST") != "1":
        pytest.skip("Set SOFIA_TTS_PLAYBACK_TEST=1 to explicitly enable speaker output")
    from PySide6.QtCore import QElapsedTimer, QEventLoop, QTimer

    from sofia.tts.native import NativeEngine, available_engines

    if backend not in available_engines():
        pytest.skip("Requested local engine is not installed")
    engine = NativeEngine(backend)
    voices = [v for v in engine.capabilities.voices if v.language == "en-US"]
    if not voices:
        engine.close()
        pytest.skip("No installed English voice")
    voice = next((v for v in voices if v.gender == "Female"), voices[0])
    events = []
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    elapsed = QElapsedTimer()
    latency = None

    def stop():
        elapsed.start()
        engine.stop()

    def event(state):
        nonlocal latency
        events.append(state)
        if state == "playing" and phase == "playing":
            QTimer.singleShot(100, stop)
        if state == "cancelled":
            latency = elapsed.elapsed()
            loop.quit()
        elif state in ("playback_failed", "unavailable", "completed"):
            loop.quit()

    try:
        engine.start(
            TTSRequest(
                "Hello from Sofia. This is a synthetic interruption test. " * 10,
                TTSConfig(True, backend, "en-US", voice.id, -0.2, 0.25),
            ),
            event,
        )
        if phase == "preparing":
            stop()
        deadline.start(10000)
        if "cancelled" not in events:
            loop.exec()
        assert events[-1] == "cancelled" and latency is not None, (
            "Stop not acknowledged"
        )
        assert latency < 2000
        print(
            f"{backend}/{phase}: Stop acknowledged in {latency} ms (not acoustic latency)"
        )
        snapshot = tuple(events)
        deadline.start(500)
        loop.exec()
        assert tuple(events) == snapshot, "Late playback after Stop"
    finally:
        deadline.stop()
        engine.stop()
        engine.close()


@pytest.mark.tts_playback
def test_real_windows_gui_queue_and_emergency_stop(qapp):
    if sys.platform != "win32" or os.environ.get("SOFIA_TTS_PLAYBACK_TEST") != "1":
        pytest.skip("Set SOFIA_TTS_PLAYBACK_TEST=1 to explicitly enable speaker output")
    from PySide6.QtCore import QEventLoop, QTimer

    from sofia.ui.main_window import MainWindow

    window = MainWindow()
    window.tts.discover()
    caps = window.tts.catalog.get("windows-local")
    if caps is None or "en-US" not in caps.languages:
        window.close()
        pytest.skip("No usable Windows English voice")
    voice = next(
        v for v in caps.voices if v.language == "en-US" and v.gender == "Female"
    )
    assert window.tts.configure(TTSConfig(True, "windows-local", "en-US", voice.id))
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    played = []
    finished = []

    def changed():
        if window.tts.state == "playing":
            played.append(window.tts.queue.active.text)
        elif window.tts.state == "completed":
            finished.append(True)
            if len(finished) == 3:
                loop.quit()

    window.tts.changed.connect(changed)
    try:
        for text in (
            "First synthetic sentence.",
            "Second synthetic sentence.",
            "Third synthetic sentence.",
        ):
            assert window.tts.speak(text)
        assert not window.tts.speak("Queue overflow.")
        deadline.start(25000)
        loop.exec()
        assert len(finished) == 3 and len(played) == 3, (
            window.tts.state,
            played,
            window.tts.queue.pending,
        )
        assert played == [
            "First synthetic sentence.",
            "Second synthetic sentence.",
            "Third synthetic sentence.",
        ]
        window.tts.changed.disconnect(changed)

        def stop_on_playing():
            if window.tts.state == "playing":
                QTimer.singleShot(100, window.stop.click)
            elif window.tts.state == "cancelled":
                loop.quit()

        window.tts.changed.connect(stop_on_playing)
        assert window.tts.speak(
            "This is a synthetic Emergency Stop demonstration. " * 10
        )
        deadline.start(10000)
        loop.exec()
        assert not window.tts.busy and not window.tts.queue.pending
        assert window.tts.state == "cancelled" and not window.speech.busy
    finally:
        deadline.stop()
        window.tts.shutdown()
        window.close()
        window.deleteLater()
