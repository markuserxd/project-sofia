"""Core imports and missing-extra behavior remain testable without Qt."""

import builtins
import subprocess
import sys


def test_ui_package_does_not_import_qt():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, sofia, sofia.ui; "
            "assert not any(n.startswith('PySide6') for n in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0


def test_missing_optional_dependency_is_explained(monkeypatch, capsys):
    from sofia.ui.app import main

    real_import = builtins.__import__

    def without_qt(name, *args, **kwargs):
        if name.startswith("PySide6"):
            raise ModuleNotFoundError("private path", name="PySide6")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_qt)
    assert main() == 2
    error = capsys.readouterr().err
    assert "pip install" in error and ".[ui]" in error
    assert "private path" not in error


def test_gui_module_entrypoint_without_qt():
    script = """
import builtins, runpy
original = builtins.__import__
def without_qt(name, *args, **kwargs):
    if name.startswith('PySide6'):
        raise ModuleNotFoundError(name='PySide6')
    return original(name, *args, **kwargs)
builtins.__import__ = without_qt
runpy.run_module('sofia.ui', run_name='__main__')
"""
    process = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert process.returncode == 2
    assert "requires" in process.stderr and not process.stdout


def test_existing_cli_stays_headless():
    for arguments in ([], ["--help"], ["--version"]):
        process = subprocess.run(
            [sys.executable, "-m", "sofia", *arguments],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert process.returncode == 0
        assert "sofia" in process.stdout.lower()
