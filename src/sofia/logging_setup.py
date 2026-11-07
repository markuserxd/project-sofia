"""Explicit JSON Lines logging for the SOFIA logger namespace."""

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from typing import TextIO, cast

from sofia.config import Settings

_TASK_ID: ContextVar[str | None] = ContextVar("sofia_task_id", default=None)
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SETUP_LOCK = Lock()
_SENSITIVE_PARTS = frozenset(
    {
        "apikey",
        "password",
        "passwd",
        "secret",
        "authorization",
        "token",
        "credential",
        "cookie",
    },
)


@contextmanager
def task_log_context(task_id: str | None) -> Iterator[None]:
    """Scope an opaque task ID, or temporarily clear it with None."""
    if task_id is not None and (
        type(task_id) is not str or not _TASK_ID_PATTERN.fullmatch(task_id)
    ):
        raise ValueError(
            "Task ID must be 1-64 ASCII letters, digits, hyphens or underscores, "
            "starting with a letter or digit."
        )
    token = _TASK_ID.set(task_id)
    try:
        yield
    finally:
        _TASK_ID.reset(token)


def _is_sensitive(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _redacted_fields(value: object, depth: int = 0) -> object:
    # Keep only paths to redactions. No caller-supplied metadata values are exported.
    # A depth bound also terminates traversal of cyclic containers.
    if depth == 8:
        return None
    if type(value) is dict:
        result: dict[str, object] = {}
        for name, item in value.items():
            if type(name) is not str:
                continue
            if _is_sensitive(name):
                result[name] = "[REDACTED]"
                continue
            child = _redacted_fields(item, depth + 1)
            if child is not None:
                result[name] = child
        return result or None
    if type(value) in (list, tuple):
        containers = cast(list[object] | tuple[object, ...], value)
        items = [_redacted_fields(item, depth + 1) for item in containers]
        return items if any(item is not None for item in items) else None
    return None


def _message(record: logging.LogRecord) -> str:
    # Avoid stringifying arbitrary objects or expanding mappings into messages.
    if type(record.msg) is not str:
        return "[Unsupported log message]"
    if type(record.args) is tuple and all(
        type(arg) in (str, int, float, bool, type(None)) for arg in record.args
    ):
        try:
            return record.getMessage()
        except (TypeError, ValueError, OverflowError):
            pass
    return "[Invalid log arguments]"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        )
        payload: dict[str, object] = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _message(record),
        }
        task_id = _TASK_ID.get()
        if task_id is not None:
            payload["task_id"] = task_id
        for name in record.__dict__:
            if type(name) is str and _is_sensitive(name):
                payload[name] = "[REDACTED]"
        metadata = _redacted_fields(getattr(record, "metadata", None))
        if metadata is not None:
            payload["metadata"] = metadata
        return json.dumps(payload)


class _JsonHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The standard fallback prints raw message arguments and exception details.
        pass


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Set up SOFIA's JSON handler, defaulting to standard error."""
    logger = logging.getLogger("sofia")
    destination = sys.stderr if stream is None else stream
    with _SETUP_LOCK:
        logger.setLevel(settings.log_level)
        logger.propagate = False
        for handler in logger.handlers:
            if isinstance(handler, _JsonHandler):
                handler.setStream(destination)
                handler.setLevel(settings.log_level)
                return
        handler = _JsonHandler(destination)
        handler.setLevel(settings.log_level)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
