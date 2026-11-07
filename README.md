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

## Offline LLM provider

The provider API uses frozen dataclasses and a structural `LLMProvider` protocol:
`generate(request)` returns an `LLMResponse`; `stream(request)` returns an iterator
of `LLMStreamChunk` objects. Both response types carry text in `content`.
Messages accept exactly `system`, `user`, or `assistant` roles and string content
(including empty text). Requests require a non-empty tuple of `LLMMessage` objects
so the conversation remains immutable. Invalid input raises `InvalidRequestError`.

```python
from sofia.providers import LLMMessage, LLMProvider, LLMRequest, MockLLMProvider

provider: LLMProvider = MockLLMProvider(response_text="Hello from SOFIA", chunk_size=6)
request = LLMRequest(
    messages=(
        LLMMessage(role="system", content="Be helpful."),
        LLMMessage(role="user", content="Hello"),
    )
)
assert provider.generate(request).content == "Hello from SOFIA"
assert (
    "".join(chunk.content for chunk in provider.stream(request)) == "Hello from SOFIA"
)
```

The mock returns its configured text for every valid conversation. Its default
response is `Hello from SOFIA`. Streaming splits text into fixed-size character
chunks (default: 6), preserving whitespace and Unicode exactly. Empty responses
produce no chunks. Calls and iterators have independent progress and no delays.
No environment variables, credentials, network services, or extra dependencies
are needed. Providers do not log prompts or generated text.

For deterministic error tests, set `failure="general"`, `"temporary"`, or
`"invalid_request"`. These raise `ProviderError`, `TemporaryProviderError`, or
`InvalidRequestError`, respectively; the latter two inherit from `ProviderError`.
Messages are fixed and contain no request, response, or environment data.
`generate()` always raises when a failure is configured. `stream()` raises on
iteration, before the first chunk by default. Set `stream_fail_after=1` to emit
one chunk before raising. If fewer chunks exist than requested, the failure is
raised after the available chunks, even for an empty response. Consumers must
handle errors through the end of iteration; a partial stream is not a success.
With `failure=None` (the default), `stream_fail_after` has no effect.
The mock has no retries or fallback routing. An optional, explicitly authorized
[OpenAI text provider](docs/openai-provider.md) supports generation and streaming
with per-run permission checks, token/cost accounting, and bounded network options.
An optional [Anthropic Claude adapter](docs/anthropic-provider.md) adds text
generation and streaming with cache-aware accounting. [Explicit model routing](docs/model-routing.md)
connects Mock, OpenAI, and Anthropic on one shared run; fallback is disabled by
default and requires trusted evidence of a failure before dispatch.

## Security guardrails

`sofia.security` provides deny-by-default scoped approvals, cooperative emergency
stop, and per-run step, token, and spending limits. Request reservations are
atomic; unknown usage and overruns block further work. These offline primitives
do not enable real APIs or tool execution. See [guardrail usage and limitations](docs/guardrails.md)
for trusted-controller boundaries, exact USD accounting, and adapter obligations.

The [typed tool registry](docs/tool-registry.md) lets trusted application code
explicitly register and discover tools, validate bounded JSON arguments, and
execute handlers using the same run permissions, step budget and cancellation.
It provides cooperative deadlines, structured results and sanitized audit events.
No tools are automatically registered or invoked by the CLI or providers.

The [bounded agent loop](docs/agent-loop.md) connects trusted or explicitly configured
JSON planners to the registry through plan, execute, observe, and verify phases.
It requires step and task verification, shares existing guardrails, and keeps finite
limits and sanitized history. Run `python examples/agent_loop.py` for offline success,
failure, and cancellation demonstrations using only in-memory arithmetic.

## Develop and test

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev,openai,anthropic]'
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/sofia
.\.venv\Scripts\python.exe -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards and package boundaries.
