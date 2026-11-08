from dataclasses import replace

import pytest
from tts_helpers import CAPS

from sofia.tts.models import TTSConfig, TTSError, TTSRequest, validate_text
from sofia.tts.queue import PlaybackQueue


def request(text="Hello"):
    return TTSRequest(text, TTSConfig(True, "fake", "en-US", "en"))


@pytest.mark.parametrize(
    "text",
    [
        None,
        1,
        b"text",
        "",
        " \n\t",
        "a" * 1001,
        "\ud800",
        "x\x00",
        "x\x1b",
        "x\u202e",
        "<speak>Hi</speak>",
        "C:\\private\\speech.wav",
        "https://example.test/",
        "/private.wav",
        "\\\\server\\voice",
    ],
)
def test_invalid_text_sanitized(text):
    with pytest.raises(TTSError, match="invalid_text"):
        validate_text(text, "en-US")


@pytest.mark.parametrize(
    "text,language",
    [
        ("สวัสดีค่ะ ฉันโซเฟีย มีอะไรให้ช่วยไหมคะ", "th-TH"),
        ("Hello! I'm Sofia. How can I help you today?", "en-US"),
        ("& harmless Markdown **hello** 😊\n", "en-GB"),
    ],
)
def test_plain_unicode(text, language):
    assert validate_text(text, language) == text


@pytest.mark.parametrize(
    "text,language,code",
    [
        ("สวัสดีค่ะ Welcome back to Project Sofia.", "th-TH", "mixed_language"),
        ("สวัสดีค่ะ", "en-US", "unsupported_language"),
        ("Hello", "th-TH", "unsupported_language"),
        ("你好", "en-US", "unsupported_language"),
        ("Hello", "xx-YY", "unsupported_language"),
    ],
)
def test_no_wrong_language_fallback(text, language, code):
    with pytest.raises(TTSError, match=code):
        validate_text(text, language)


@pytest.mark.parametrize(
    "changes",
    [
        dict(enabled=1),
        dict(engine=None),
        dict(rate=True),
        dict(rate=float("nan")),
        dict(rate=float("inf")),
        dict(rate=1.1),
        dict(volume=-0.01),
        dict(volume=1.01),
        dict(pitch="warm"),
        dict(voice="a" * 257),
    ],
)
def test_bounded_settings(changes):
    with pytest.raises(TTSError, match="invalid_settings"):
        TTSConfig(**changes)


@pytest.mark.parametrize(
    "changes,code",
    [
        (dict(engine="remote"), "unknown_engine"),
        (dict(language="en-GB"), "unsupported_language"),
        (dict(voice="imaginary"), "missing_voice"),
        (dict(voice="th"), "missing_voice"),
        (dict(rate=0.6), "unsupported_rate"),
        (dict(pitch=0.1), "unsupported_pitch"),
    ],
)
def test_capabilities_validate(changes, code):
    with pytest.raises(TTSError, match=code):
        CAPS.validate(replace(request().config, **changes))


def test_capabilities_honest_and_offline():
    assert CAPS.languages == ("en-US", "th-TH")
    assert CAPS.validate(request().config).gender == "Female"
    assert CAPS.voices[1].gender == "Male"
    assert not CAPS.network_required and CAPS.installed
    assert not CAPS.incremental and CAPS.pitch_range is None
    with pytest.raises(TTSError, match="unknown_engine"):
        replace(CAPS, network_required=True).validate(request().config)
    with pytest.raises(TTSError, match="unsupported_rate"):
        replace(CAPS, rate_range=None).validate(replace(request().config, rate=0.1))


def test_fifo_single_active_and_ids():
    queue = PlaybackQueue()
    first, second, third = (request(str(i)) for i in range(3))
    assert len({first.id, second.id, third.id}) == 3
    for item in (first, second, third):
        queue.enqueue(item)
    assert queue.start_next() is first
    assert queue.start_next() is first
    assert not queue.complete(second.id)
    assert queue.complete(first.id)
    assert queue.start_next() is second
    assert queue.complete(second.id)
    assert queue.start_next() is third


def test_queue_count_and_total_bounds():
    queue = PlaybackQueue()
    queue.enqueue(request())
    queue.start_next()
    for _ in range(3):
        queue.enqueue(request())
    with pytest.raises(TTSError, match="queue_full"):
        queue.enqueue(request())
    aggregate = PlaybackQueue()
    for _ in range(3):
        aggregate.enqueue(request("a" * 1000))
    aggregate.start_next()
    with pytest.raises(TTSError, match="queue_full"):
        aggregate.enqueue(request("x"))


def test_duplicate_clear_replace_and_old_completion():
    queue = PlaybackQueue()
    first = request()
    queue.enqueue(first)
    with pytest.raises(TTSError):
        queue.enqueue(first)
    queue.start_next()
    queue.enqueue(request("old pending"))
    latest = request("replacement")
    queue.replace_pending(latest)
    assert queue.active is first and queue.pending == 1
    queue.complete(first.id)
    assert queue.start_next() is latest
    assert not queue.complete(first.id)
    queue.clear()
    assert queue.active is latest
    assert "Hello" not in repr(first)
