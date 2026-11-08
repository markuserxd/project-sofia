"""Deterministic devices and recognizers; never open hardware or a model."""

from array import array
from threading import Event

from PySide6.QtCore import QObject, Signal

from sofia.speech.audio import AudioFormat, Recording, SpeechError
from sofia.speech.transcription import Transcription


def recording(rate=16000, channels=1, seconds=0.5):
    return Recording(
        array("h", [1000, -1000] * int(rate * seconds * channels / 2)).tobytes(),
        AudioFormat(rate, channels, "i16"),
    )


class FakeCapture(QObject):
    started = Signal()
    availability_changed = Signal(bool)
    failed = Signal(str)
    limit_reached = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.starts = 0
        self.stops = 0
        self.active = False
        self.available_device = True
        self.delayed = False
        self.error = ""
        self.audio = recording()

    def available(self):
        return self.available_device

    def start(self):
        self.starts += 1
        if self.error:
            self.failed.emit(self.error)
        else:
            self.active = True
            if not self.delayed:
                self.started.emit()

    def finish(self):
        self.stops += 1
        self.active = False
        return self.audio

    def discard(self):
        self.active = False


class FakeRecognizer:
    def __init__(self, text="สวัสดีโซเฟีย Hello Sofia"):
        self.text = text
        self.calls = []
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.error = ""

    def transcribe(self, audio, language, cancel):
        self.calls.append((language, cancel))
        self.entered.set()
        assert self.release.wait(3)
        if self.error:
            raise SpeechError(self.error)
        # Deliberately return even after cancellation to test the controller gate.
        return Transcription(self.text, language)
