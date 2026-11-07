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

## Configuration and logging

Running `python -m sofia` with no options validates settings and initializes
logging before displaying help. Invalid settings produce a brief message on
standard error and exit with code 2. Explicit `--help` and `--version` work
independently of startup settings.

| Environment variable | Default | Accepted values |
| --- | --- | --- |
| `SOFIA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `SOFIA_PROVIDER` | `mock` | `mock` |

Set overrides in PowerShell with commands such as `$env:SOFIA_LOG_LEVEL = 'DEBUG'`.
SOFIA reads the process environment. `.env.example` is a reference. Values are
case-insensitive and leading or trailing whitespace is ignored. The mock
setting requires no provider credentials or network access.

`configure_logging(load_settings())` enables JSON Lines on standard error for
the `sofia` logger and its descendants. Each record has a UTC `timestamp`,
`level`, `logger`, and `message`. Wrap task work in `with task_log_context(task_id):`
to add a `task_id` to its logs and restore the previous context on exit. Use
opaque IDs made of 1–64 ASCII letters, digits, hyphens, or underscores, starting
with a letter or digit.

The formatter omits exception details, stack traces, and arbitrary record
extras. Recognized sensitive fields are replaced with `[REDACTED]`, including
fields inside plain dictionaries, lists, and tuples supplied via
`extra={"metadata": ...}`. Metadata preserves only paths to redacted fields
within eight levels. Use application-defined field names. Free-form messages,
format arguments, and task IDs can contain secrets that redaction cannot
reliably detect. Keep credentials, raw environment data, user content, and
external payloads out of log calls.

## Develop and test

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/sofia
.\.venv\Scripts\python.exe -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards and package boundaries.
