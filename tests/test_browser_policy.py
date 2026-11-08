from dataclasses import FrozenInstanceError, replace

import pytest

from examples.browser_test_server import fixture_policy
from sofia.tools.browser_policy import (
    BrowserConfig,
    BrowserElement,
    BrowserError,
    BrowserOriginPolicy,
    safe_filename,
)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "http://localhost:8123",
        "http://127.1:8123",
        "http://2130706433:8123",
        "http://127.0.0.1:80",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:08123",
        "http://[::1]:8123",
        "http://[::ffff:127.0.0.1]:8123",
        "http://169.254.169.254:8123",
        "http://10.0.0.1:8123",
        "http://127.0.0.1.evil.test:8123",
        "http://user@127.0.0.1:8123",
        "http://*.test:8123",
        "http://\u212a.test:8123",
        "http://127.0.0.1:8123/",
        "file:///tmp/x",
    ],
)
def test_unsupported_topologies_fail_closed(origin):
    with pytest.raises(BrowserError):
        BrowserOriginPolicy("fixture", origin, ())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8123/",
        "http://127.0.0.1:8124/",
        "http://127.0.0.2:8123/",
        "http://user:pass@127.0.0.1:8123/",
        "http://127.0.0.1:8123/?secret=x",
        "http://127.0.0.1:8123/#x",
        "http://127.0.0.1:8123/%2e%2e/",
        "http://127.0.0.1:8123/../",
        "http://127.0.0.1:8123/\\evil",
        "http://127.0.0.1:8123/\n",
        "//evil.test/",
        "javascript:alert(1)",
        "data:text/html,x",
        "blob:http://127.0.0.1:8123/x",
        "ftp://127.0.0.1:8123/",
        "chrome://settings",
    ],
)
def test_exact_origin_and_strict_path(url):
    policy = fixture_policy("http://127.0.0.1:8123")
    assert policy.path(policy.origin + "/next") == "/next"
    with pytest.raises(BrowserError):
        policy.path(url)


@pytest.mark.parametrize(
    "name",
    [
        "../x.txt",
        "x/y.txt",
        "x\\y.txt",
        "C:\\x.txt",
        "CON.txt",
        "lpt1.txt",
        "x.txt.exe",
        "x.exe",
        "x.txt:ads",
        "x..txt",
        "x.txt ",
        "x\n.txt",
        ".txt",
        "a.zip",
    ],
)
def test_untrusted_filename(name):
    with pytest.raises(BrowserError):
        safe_filename(name)
    assert safe_filename("fixture.txt") == "fixture.txt"


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout": 0},
        {"timeout": float("inf")},
        {"session_ttl": float("nan")},
        {"preview_ttl": True},
        {"max_sessions": 0},
        {"max_operations": 1000},
        {"max_text": 0},
        {"max_pending": False},
        {"max_downloads": -1},
        {"origins": []},
    ],
)
def test_invalid_limits(changes):
    with pytest.raises(BrowserError):
        replace(BrowserConfig(), **changes)


def test_default_denial_and_immutable_selectors():
    assert BrowserConfig().origins == ()
    with pytest.raises(BrowserError):
        BrowserElement("x", "input[type=password]", "fill")
    policy = fixture_policy("http://127.0.0.1:8123")
    with pytest.raises(FrozenInstanceError):
        policy.origin = "https://evil.test"
    with pytest.raises(BrowserError):
        BrowserConfig((policy, policy))
