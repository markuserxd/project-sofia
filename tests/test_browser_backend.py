import io
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from examples.browser_test_server import fixture_policy
from sofia.security import Cancellation, OperationCancelled
from sofia.tools import ToolContext
from sofia.tools.browser_backend import PlaywrightBrowserBackend, fixture_request
from sofia.tools.browser_policy import BrowserError


def test_manifest_cannot_enable_arbitrary_scripts_or_webrtc():
    import base64
    import hashlib

    from sofia.tools.browser_policy import FIXTURE_SCRIPT

    allowed = base64.b64encode(hashlib.sha256(FIXTURE_SCRIPT).digest()).decode()
    csp = PlaywrightBrowserBackend._csp()
    assert allowed in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'none'" in csp and "worker-src 'none'" in csp
    assert (
        "sha256-"
        + base64.b64encode(hashlib.sha256(b"new RTCPeerConnection()").digest()).decode()
        not in csp
    )


def test_event_errors_are_latched_without_raw_logging(caplog):
    backend = PlaywrightBrowserBackend()

    def failing():
        raise RuntimeError("private callback payload")

    backend._event("s", failing)
    assert "s" in backend._faults
    assert "private callback payload" not in caplog.text


def context():
    return ToolContext(Cancellation(), monotonic() + 5, monotonic)


@pytest.mark.parametrize(
    "status,length,body,encoding",
    [
        (302, "0", b"", None),
        (307, "0", b"", None),
        (500, "3", b"abc", None),
        (200, "100000000", b"abc", None),
        (200, None, b"abc", None),
        (200, "3", b"xyz", None),
        (200, "3", b"abc", "gzip"),
    ],
)
def test_broker_rejects_redirects_unbounded_or_unreviewed_responses(
    monkeypatch, status, length, body, encoding
):
    response = Mock(status=status)
    response.getheader.side_effect = lambda name: {
        "Content-Length": length,
        "Content-Encoding": encoding,
    }.get(name)
    response.read1.side_effect = io.BytesIO(body).read
    connection = Mock(sock=None)
    connection.getresponse.return_value = response
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(
        "sofia.tools.browser_backend.http.client.HTTPConnection", constructor
    )
    with pytest.raises(BrowserError):
        fixture_request(
            fixture_policy("http://127.0.0.1:8123"), "/", b"abc", context(), 1
        )
    constructor.assert_called_once()
    assert constructor.call_args.args == ("127.0.0.1", 8123)
    assert connection.request.call_count == 1
    connection.close.assert_called_once()


def test_broker_streaming_cancellation_and_no_proxy(monkeypatch):
    response = Mock(status=200)
    response.getheader.side_effect = lambda name: (
        "3" if name == "Content-Length" else None
    )
    connection = Mock(sock=None)
    connection.getresponse.return_value = response
    ctx = context()

    def chunk(size):
        ctx._cancellation.cancel()
        return b"a"

    response.read1.side_effect = chunk
    monkeypatch.setattr(
        "sofia.tools.browser_backend.http.client.HTTPConnection",
        lambda *a, **kw: connection,
    )
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.test")
    with pytest.raises(OperationCancelled):
        fixture_request(fixture_policy("http://127.0.0.1:8123"), "/", b"abc", ctx, 1)
    connection.close.assert_called_once()
    assert "Cookie" not in connection.request.call_args.kwargs["headers"]


def test_default_request_and_foreign_frame_never_continued():
    backend = PlaywrightBrowserBackend()
    frame = object()
    backend._sessions["s"] = (Mock(), SimpleNamespace(main_frame=frame))
    route = Mock(request=SimpleNamespace(frame=frame))
    backend._route("s", route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()
    route.fetch.assert_not_called()
    callback = Mock()
    backend._tickets["s"] = callback
    route = Mock(request=SimpleNamespace(frame=object()))
    backend._route("s", route)
    callback.assert_not_called()
    route.abort.assert_called_once()
    assert not backend._tickets


def test_ticket_exact_destination_method_payload_and_one_use():
    backend = PlaywrightBrowserBackend()
    frame = object()
    backend._sessions["s"] = (Mock(), SimpleNamespace(main_frame=frame))
    route = Mock(
        request=SimpleNamespace(
            frame=frame,
            url="http://127.0.0.1:8123/other",
            method="POST",
            post_data_buffer=b"different",
        )
    )
    with pytest.raises(BrowserError, match="request_denied"):
        backend._transfer(
            "s",
            fixture_policy("http://127.0.0.1:8123"),
            "/",
            b"abc",
            context(),
            lambda: backend._route("s", route),
            payload=b"approved",
        )
    route.abort.assert_called_once()
    assert not backend._tickets


@pytest.mark.parametrize(
    "change", ["missing", "multiple", "hidden", "disabled", "editable", "frames"]
)
def test_element_fail_closed(change):
    backend = PlaywrightBrowserBackend()
    target = Mock()
    target.count.return_value = 1
    target.is_visible.return_value = target.is_enabled.return_value = True
    target.get_attribute.return_value = None
    page = SimpleNamespace(frames=[object()], get_by_test_id=lambda _: target)
    if change == "missing":
        target.count.return_value = 0
    elif change == "multiple":
        target.count.return_value = 2
    elif change == "hidden":
        target.is_visible.return_value = False
    elif change == "disabled":
        target.is_enabled.return_value = False
    elif change == "editable":
        target.get_attribute.return_value = "true"
    else:
        page.frames.append(object())
    backend._sessions["s"] = (Mock(), page)
    with pytest.raises(BrowserError):
        backend._element("s", "fixed", context())


def test_cleanup_attempts_all_resources_and_reports_fixed_error():
    backend = PlaywrightBrowserBackend()
    bad = Mock()
    bad.close.side_effect = RuntimeError("private raw exception")
    good, browser, driver = Mock(), Mock(), Mock()
    backend._sessions = {"bad": (bad, Mock()), "good": (good, Mock())}
    backend._browser, backend._driver = browser, driver
    with pytest.raises(BrowserError, match="cleanup_failed"):
        backend.close_all()
    good.close.assert_called_once()
    browser.close.assert_called_once()
    driver.stop.assert_called_once()
