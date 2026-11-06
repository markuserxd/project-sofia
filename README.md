# Project SOFIA

SOFIA is a Windows-first project for an autonomous AI desktop assistant. The
current release provides the Python package and a command-line interface for
displaying help and version information.

## Install on Windows

You need Python 3.11 or newer with `python` available in PowerShell. From the
repository root, create a virtual environment and install SOFIA in editable mode.
The commands use the virtual environment's Python directly, so activation is optional.

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Use the CLI

Both commands run offline and require no API credentials or environment file.

```powershell
.\.venv\Scripts\python.exe -m sofia --help
.\.venv\Scripts\python.exe -m sofia --version
```

The install also provides `sofia.exe` in `.venv\Scripts`.

## Develop and test

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards and package boundaries.
