import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
from threading import Barrier

import pytest

from sofia.config import Settings
from sofia.logging_setup import configure_logging, task_log_context


@pytest.fixture
def log_output() -> Iterator[StringIO]:
    logger = logging.getLogger("sofia")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    output = StringIO()
    configure_logging(Settings(), output)
    try:
        yield output
    finally:
        for handler in logger.handlers[:]:
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_json_lines_with_utc_timestamp_and_level_filtering(log_output: StringIO) -> None:
    logger = logging.getLogger("sofia.core")
    logger.debug("Hidden")
    logger.info("Started %d task\nready", 1)
    assert len(log_output.getvalue().splitlines()) == 1
    record = json.loads(log_output.getvalue())
    timestamp = record.pop("timestamp")
    assert timestamp.endswith("Z")
    assert datetime.fromisoformat(timestamp).utcoffset() == timedelta(0)
    assert record == {"level": "INFO", "logger": "sofia.core", "message": "Started 1 task\nready"}


def test_reinitialization_changes_level_and_stream_without_duplicates(log_output: StringIO) -> None:
    output = StringIO()
    configure_logging(Settings(log_level="DEBUG"), output)
    configure_logging(Settings(log_level="DEBUG"), output)
    logging.getLogger("sofia.core").debug("Visible once")
    assert log_output.getvalue() == ""
    assert len(output.getvalue().splitlines()) == 1
    assert json.loads(output.getvalue())["level"] == "DEBUG"


def test_task_context_isolation_and_restoration(log_output: StringIO) -> None:
    logger = logging.getLogger("sofia.core")
    both_tasks_active = Barrier(2, timeout=5)

    def worker(task_id: str) -> None:
        with task_log_context(task_id):
            both_tasks_active.wait()
            logger.info("Running %s", task_id)
            with pytest.raises(RuntimeError), task_log_context(None):
                logger.info("Temporarily cleared")
                raise RuntimeError
            logger.info("Restored %s", task_id)
        logger.info("Finished")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, ["task-1", "task-2"]))
    records = [json.loads(line) for line in log_output.getvalue().splitlines()]
    assert len(records) == 8
    for record in records:
        message = record["message"]
        if message.startswith(("Running", "Restored")):
            assert record["task_id"] == message.split()[-1]
        else:
            assert "task_id" not in record


@pytest.mark.parametrize("task_id", ["", "-invalid", "test-private\nvalue", "x" * 65])
def test_invalid_task_id_is_rejected_safely(task_id: str) -> None:
    with pytest.raises(ValueError, match="^Task ID must be 1-64 ASCII") as error:
        with task_log_context(task_id):
            pytest.fail("Invalid ID was accepted")
    if task_id:
        assert task_id not in str(error.value)


def test_redacts_sensitive_fields_and_omits_raw_payloads(log_output: StringIO) -> None:
    logger = logging.getLogger("sofia.core")
    private = "test-private-value"
    logger.info(
        "Connection initialized",
        extra={
            "api_key": private,
            "Authorization": private,
            "config": Settings(),
            "metadata": {
                "environment": {"PRIVATE_PATH": private, "OPENAI_API_KEY": private},
                "upstream": [{"clientSecret": private, "refresh-token": private, "raw": private}],
                "password": {"nested": private},
            },
        },
    )
    output = log_output.getvalue()
    record = json.loads(output)
    assert record["api_key"] == record["Authorization"] == "[REDACTED]"
    assert record["metadata"] == {
        "environment": {"OPENAI_API_KEY": "[REDACTED]"},
        "upstream": [{"clientSecret": "[REDACTED]", "refresh-token": "[REDACTED]"}],
        "password": "[REDACTED]",
    }
    assert private not in output
    assert "config" not in record


def test_malformed_records_do_not_expose_objects_or_exception_details(
    log_output: StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unexpected:
        def __str__(self) -> str:
            raise AssertionError("test-private-value")

    # Pytest also attaches its own formatters to non-propagating loggers.
    sofia_logger = logging.getLogger("sofia")
    sofia_handlers = [
        handler for handler in sofia_logger.handlers if getattr(handler, "stream", None) is log_output
    ]
    monkeypatch.setattr(sofia_logger, "handlers", sofia_handlers)
    logger = logging.getLogger("sofia.core")
    cyclic: dict[str, object] = {}
    cyclic["cycle"] = cyclic
    logger.info(Unexpected(), extra={"metadata": cyclic})
    logger.info("Config %s", Settings())
    logger.info("Password %(password)s", {"password": "test-private-value"})
    logger.info("Invalid number %d", "test-private-value")
    try:
        raise ValueError("test-private-value")
    except ValueError:
        logger.exception("Operation failed", stack_info=True)

    output = log_output.getvalue()
    assert "test-private-value" not in output
    assert "Traceback" not in output
    assert [json.loads(line)["message"] for line in output.splitlines()] == [
        "[Unsupported log message]",
        "[Invalid log arguments]",
        "[Invalid log arguments]",
        "[Invalid log arguments]",
        "Operation failed",
    ]


def test_stream_error_never_prints_raw_logging_diagnostics(
    log_output: StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    class BrokenStream(StringIO):
        def write(self, data: str) -> int:
            raise OSError("test-private-value")

    configure_logging(Settings(), BrokenStream())
    logging.getLogger("sofia.core").info("Ignored")
    assert capsys.readouterr().err == ""
