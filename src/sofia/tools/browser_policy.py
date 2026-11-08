"""Immutable controller-owned contracts for the synthetic loopback browser mode."""

import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sofia.tools.schemas import valid_name

# The supported mode has exactly one reviewed renderer script. Manifest bytes
# authorize content, never arbitrary executable website code (including WebRTC).
FIXTURE_SCRIPT = b"""
document.querySelector('[data-testid="benign"]').addEventListener('click', () => {
document.querySelector('[data-testid="status"]').textContent = 'changed';
});
"""


class BrowserError(ValueError):
    """Only internal fixed codes; never include browser or HTTP exception text."""

    _codes = frozenset(
        {
            "browser_dependency_missing",
            "browser_launch_failed",
            "cleanup_failed",
            "fixture_response_denied",
            "fixture_unavailable",
            "request_denied",
            "page_changed",
            "unknown_document",
            "ambiguous_target",
            "unsafe_target",
            "observation_limit",
            "unsafe_form",
            "submission_unverified",
            "download_unverified",
            "invalid_config",
            "unsupported_origin",
            "invalid_url",
            "origin_denied",
            "unsafe_filename",
            "exclusive_access_required",
            "unsupported_operation",
            "preview_not_found",
            "stale_preview",
            "invalid_registration",
            "unknown_session",
            "session_limit",
            "browser_closed",
            "preview_mismatch",
            "page_not_loaded",
            "unsafe_text",
            "interaction_unverified",
            "preview_limit",
            "download_limit",
            "download_disabled",
            "write_failed",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = (
            code if type(code) is str and code in self._codes else "browser_failed"
        )
        super().__init__(self.code)


def fixture_origin(value: str) -> str:
    # Numeric IPv4 only: no DNS, IDNA, proxy, rebinding or alternative IP spelling.
    if type(value) is not str or not re.fullmatch(
        r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", value
    ):
        raise BrowserError("unsupported_origin")
    try:
        port = urlsplit(value).port
    except ValueError:
        raise BrowserError("unsupported_origin") from None
    if port is None or not 1024 <= port <= 65535:
        raise BrowserError("unsupported_origin")
    return value


def fixture_path(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"/[A-Za-z0-9_/-]{0,120}", value):
        raise BrowserError("invalid_url")
    if "//" in value:
        raise BrowserError("invalid_url")
    return value


def safe_filename(value: str) -> str:
    if (
        type(value) is not str
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}\.txt", value)
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise BrowserError("unsafe_filename")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class FixtureDocument:
    path: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        fixture_path(self.path)
        if type(self.body) is not bytes or not 1 <= len(self.body) <= 65536:
            raise BrowserError("invalid_config")


@dataclass(frozen=True, slots=True)
class BrowserElement:
    key: str
    test_id: str
    operation: str
    status_key: str = ""
    expected_text: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if (
            not valid_name(self.key)
            or not valid_name(self.test_id)
            or self.operation not in ("observe", "fill", "click")
            or type(self.expected_text) is not str
            or len(self.expected_text) > 256
            or (self.operation == "click" and not valid_name(self.status_key))
        ):
            raise BrowserError("invalid_config")


@dataclass(frozen=True, slots=True, repr=False)
class BrowserForm:
    key: str
    test_id: str
    submit_test_id: str
    fields: tuple[str, ...]
    destination: str
    response: FixtureDocument

    def __post_init__(self) -> None:
        fixture_path(self.destination)
        if (
            not all(
                valid_name(x) for x in (self.key, self.test_id, self.submit_test_id)
            )
            or type(self.fields) is not tuple
            or not 1 <= len(self.fields) <= 8
            or not all(valid_name(x) for x in self.fields)
            or len(set(self.fields)) != len(self.fields)
            or type(self.response) is not FixtureDocument
            or self.response.path != self.destination
        ):
            raise BrowserError("invalid_config")


@dataclass(frozen=True, slots=True, repr=False)
class BrowserDownload:
    key: str
    test_id: str
    path: str
    filename: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        fixture_path(self.path)
        safe_filename(self.filename)
        if (
            not valid_name(self.key)
            or not valid_name(self.test_id)
            or type(self.body) is not bytes
            or not 1 <= len(self.body) <= 4096
        ):
            raise BrowserError("invalid_config")
        try:
            if any(
                not (c.isprintable() or c in "\r\n\t")
                for c in self.body.decode("utf-8")
            ):
                raise BrowserError("invalid_config")
        except UnicodeError:
            raise BrowserError("invalid_config") from None


@dataclass(frozen=True, slots=True, repr=False)
class BrowserOriginPolicy:
    """Reviewed fixture manifest, never a general website or a model-provided manifest."""

    origin_id: str
    origin: str
    documents: tuple[FixtureDocument, ...]
    elements: tuple[BrowserElement, ...] = ()
    forms: tuple[BrowserForm, ...] = ()
    downloads: tuple[BrowserDownload, ...] = ()

    def __post_init__(self) -> None:
        fixture_origin(self.origin)
        if not valid_name(self.origin_id):
            raise BrowserError("invalid_config")
        for items, cls, attr, limit in (
            (self.documents, FixtureDocument, "path", 16),
            (self.elements, BrowserElement, "key", 16),
            (self.forms, BrowserForm, "key", 4),
            (self.downloads, BrowserDownload, "key", 4),
        ):
            if (
                type(items) is not tuple
                or len(items) > limit
                or any(type(x) is not cls for x in items)
                or len({getattr(x, attr) for x in items}) != len(items)
            ):
                raise BrowserError("invalid_config")
        observations = {x.key for x in self.elements if x.operation == "observe"}
        if any(
            x.operation == "click" and x.status_key not in observations
            for x in self.elements
        ):
            raise BrowserError("invalid_config")

    def path(self, url: str) -> str:
        if (
            type(url) is not str
            or len(url) > 256
            or any(ord(c) < 33 or ord(c) > 126 for c in url)
        ):
            raise BrowserError("invalid_url")
        try:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise BrowserError("invalid_url")
            if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
                raise BrowserError("origin_denied")
            return fixture_path(parsed.path)
        except ValueError:
            raise BrowserError("invalid_url") from None


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    origins: tuple[BrowserOriginPolicy, ...] = ()
    timeout: float = 5.0
    session_ttl: float = 120.0
    preview_ttl: float = 30.0
    max_sessions: int = 2
    max_operations: int = 64
    max_text: int = 256
    max_pending: int = 16
    max_downloads: int = 2

    def __post_init__(self) -> None:
        if (
            type(self.origins) is not tuple
            or len(self.origins) > 4
            or any(type(x) is not BrowserOriginPolicy for x in self.origins)
            or len({x.origin_id for x in self.origins}) != len(self.origins)
            or len({x.origin for x in self.origins}) != len(self.origins)
        ):
            raise BrowserError("invalid_config")
        for value, upper in (
            (self.timeout, 15),
            (self.session_ttl, 300),
            (self.preview_ttl, 120),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= upper
            ):
                raise BrowserError("invalid_config")
        for value, upper in (
            (self.max_sessions, 4),
            (self.max_operations, 128),
            (self.max_text, 512),
            (self.max_pending, 32),
            (self.max_downloads, 4),
        ):
            if type(value) is not int or not 1 <= value <= upper:
                raise BrowserError("invalid_config")
