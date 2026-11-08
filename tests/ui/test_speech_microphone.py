"""Explicitly opted-in hardware capture; no recording archives or inference."""

import os
import sys

import pytest
from PySide6.QtTest import QTest

from sofia.speech.capture import AudioCapture


@pytest.mark.microphone
def test_real_windows_microphone(qapp, wait):
    if sys.platform != "win32" or os.environ.get("SOFIA_MICROPHONE_TEST") != "1":
        pytest.skip("Requires explicit Windows microphone capture consent")
    microphone = AudioCapture()
    started, errors = [], []
    microphone.started.connect(lambda: started.append(True))
    microphone.failed.connect(errors.append)
    try:
        assert microphone.available(), "No microphone available"
        microphone.start()
        wait(lambda: bool(started or errors))
        assert not errors, "Microphone failed (check device/privacy settings)"
        QTest.qWait(500)
        assert not errors
        audio = microphone.finish()
        assert audio.pcm
    finally:
        microphone.discard()
