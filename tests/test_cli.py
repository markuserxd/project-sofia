import subprocess
import sys
from importlib.metadata import version

import pytest

import sofia


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sofia", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_package_import() -> None:
    assert sofia.__name__ == "sofia"


def test_cli_help() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "usage: sofia" in result.stdout
    assert "--version" in result.stdout
    assert result.stderr == ""


def test_cli_version() -> None:
    result = run_cli("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == f"sofia {version('project-sofia')}"
    assert result.stderr == ""


def test_cli_rejects_unknown_option() -> None:
    result = run_cli("--unknown-option")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unrecognized arguments: --unknown-option" in result.stderr


def test_default_startup_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SOFIA_LOG_LEVEL",
        "SOFIA_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    result = run_cli()
    assert result.returncode == 0
    assert result.stdout == run_cli("--help").stdout
    assert result.stderr == ""


def test_invalid_configuration_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOFIA_LOG_LEVEL", "test-private-value")
    result = run_cli()
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "sofia: Invalid SOFIA_LOG_LEVEL. Expected one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
    )
    assert run_cli("--help").returncode == 0
    assert run_cli("--version").returncode == 0
    assert "unrecognized arguments" in run_cli("--unknown-option").stderr
