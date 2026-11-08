"""Deterministic speech engine: no Qt object, device, files or network."""

from sofia.tts.models import Capabilities, Voice

CAPS = Capabilities(
    "fake",
    (
        Voice("en", "Synthetic English", "en-US", "fake", "Female"),
        Voice("th", "Synthetic Thai", "th-TH", "fake", "Male"),
    ),
    rate_range=(-0.5, 0.5),
)


class FakeEngine:
    capabilities = CAPS

    def __init__(self):
        self.request = None
        self.callback = None
        self.stops = 0
        self.closed = False
        self.delayed_stop = False

    def start(self, request, callback):
        self.capabilities.validate(request.config)
        self.request = request
        self.callback = callback
        callback("preparing")

    def emit(self, event):
        self.callback(event)

    def stop(self):
        self.stops += 1
        if not self.delayed_stop:
            self.emit("cancelled")

    def close(self):
        self.closed = True
