# Opt-in OpenAI text provider

The optional adapter implements the unchanged synchronous `LLMProvider` protocol:
`generate(LLMRequest) -> LLMResponse` and `stream(LLMRequest) -> Iterator[LLMStreamChunk]`.
It uses the official Python SDK's `client.responses.create`, including SSE
streaming, over the standard OpenAI endpoint. No calls occur on import or
construction. Mock remains the default and the CLI never starts live generation.

## Install and configure

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev,openai]'
# Supply OPENAI_API_KEY through your process/secret manager, never source code.
```

`openai>=2.54,<3` and `httpx>=0.28,<1` are isolated in the `openai` extra. Import
the adapter explicitly from `sofia.providers.openai_provider`; the normal
`sofia.providers` import does not load either dependency. The CLI setting
`SOFIA_PROVIDER=mock` is unchanged. Merely setting a key grants no spending authority.

Trusted application code must supply a shared `RunGuardrails`, an exact model
identifier, verified pricing, a local input-token upper-bound callable, and a
positive output cap (Responses requires at least 16). Missing configuration fails
closed. No model discovery or remote token-count call is made. Prefer a pinned
model snapshot: the response model must match the configured/priced identifier
exactly. Aliases that return a different snapshot fail closed.

This example uses a deliberately loose, model-wide input bound, with no tokenizer
dependency. The documented context window bounds accepted input, including message
framing; oversized requests are rejected by the API with truncation disabled.
Reserving the full window is conservative and requires a correspondingly large
token budget even for short conversations. A smaller bound is acceptable only
when trusted local code can justify it for the exact request and model, including
all framing. Character counts and visible response text are not exact token counts.

```python
from decimal import Decimal

from sofia.providers import LLMMessage, LLMRequest
from sofia.providers.openai_accounting import ModelPricing
from sofia.providers.openai_provider import OpenAIConfig, OpenAIProvider
from sofia.security import Action, RunGuardrails

model = "gpt-4.1-mini-2025-04-14"
# Example values verified 2026-11-07; recheck the official model/pricing pages.
# There is no automatic price table or permanent price guarantee.
config = OpenAIConfig(
    model=model,
    pricing=ModelPricing(model, Decimal("0.40"), Decimal("1.60"), Decimal("0.10")),
    input_token_bound=lambda request: 1_047_576,
    max_output_tokens=256,
    connect_timeout=5.0,
    io_timeout=30.0,
    max_attempts=1,
)
run = RunGuardrails(max_steps=2, max_tokens=1_100_000, max_usd=Decimal("0.50"))
provider = OpenAIProvider(config=config, run=run)
try:
    # Trusted controller only, AFTER human approval of this exact paid scope.
    run.permissions.approve(Action.API_REQUEST, provider.permission_scope)
    response = provider.generate(LLMRequest((LLMMessage("user", "Hello"),)))
    # Use response.content in the application's UI; keep it out of logs.
    usage_totals = run.budget.snapshot
finally:
    provider.close()
```

The example is an explicitly paid operation if executed after approval. Tests
never execute it. See the [official model description](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
for the model's window/output limits and the
[pricing documentation](https://developers.openai.com/api/docs/pricing) for current
rates. Other Responses text models require their own verified bound, output-cap
support, and exact pricing record. Multimodal, tools, background jobs, conversation
storage, routing, and resumption are outside this adapter's scope.

## Authorization and accounting

Every attempt validates the request, checks cancellation, obtains the trusted
bound, computes a conservative uncached-input/output reservation, and calls
`run.begin_request("openai:<configured-model>", ...)`. This atomically consumes a
step and reserves both tokens and USD. Permission and cancellation are rechecked
immediately before SDK dispatch. The provider never creates a run or approves a
scope. Share one run across providers and child operations; never reset it to
continue a blocked task. Existing locks prevent concurrent oversubscription.

Pricing uses positive, finite `Decimal` USD-per-million rates. Computation uses
exact rational representations of those Decimals, then rounds upward once to
micro-USD, independently of the caller's Decimal precision. The same rounding is
used for recorded costs because the ledger cannot represent fractional micro-USD.
For actual usage the formula is:

```text
USD = ((input - cached) * input_rate + cached * cached_rate
       + output * output_rate) / 1_000_000
```

Actual input, output (including reasoning), total, and cache counts come from
validated SDK usage. Booleans, negative/fractional counts, inconsistent totals,
and impossible cache/reasoning counts are rejected. Cached tokens are a subset
of input and are never counted twice. Missing cache pricing with nonzero cached
usage records known total tokens but marks cost uncertain. Cache-write charges
are unsupported and fail closed when reported. Configure rates that cover all
applicable context-length tiers; a single rate record cannot model tiered prices
exactly and should conservatively use their maximum, or reject that model.

Settlement occurs exactly once per reservation, including failures and iterator
cleanup. Valid usage replaces the reservation. Unknown usage retains the
reservation estimate and permanently blocks the run. Overruns record all known
usage/cost and block further work even below the overall run cap. Provider or
cancellation failures remain their original sanitized SOFIA type; accounting
uncertainty is also exposed as an `AccountingBlocked` cause and in the snapshot.
An otherwise successful request that overruns raises `AccountingBlocked` directly.
Accounting errors are never turned into successful text.

The snapshot exposes aggregate tokens and spending. Sanitized `sofia.providers.openai`
usage logs record input/output/cached/total counts and rounded USD per attempt,
with the existing opaque task context if supplied. There is no mutable `last_usage`.
Unknown charges are estimates, not confirmed provider charges or invoice totals.

## Timeouts, retries, streaming, and cleanup

Explicit connect and read/write/pool timeouts default to 5 and 30 seconds.
They are socket-operation/idle limits, not a hard wall-clock deadline for an
entire response. The SDK's built-in retries are disabled, including on injected
SDK clients. `max_attempts` is 1 by default and accepts only 1–3.

Only SDK failures caused directly by HTTPX `ConnectError`, `ConnectTimeout`, or
`PoolTimeout` are classified as before billable dispatch. These settle with known
zero usage, retain their consumed step, and may retry after cancellation and
permission/budget checks. No retry sleeps are used. Authentication, request,
rate-limit, server, read/write, and ambiguous connection failures never replay;
unknown billing blocks the run. Stream iteration is never restarted.

Streaming preserves text deltas, including empty strings and Unicode. Success
requires a completed terminal response, valid usage, agreement between deltas
and final text, successful accounting, and resource cleanup. Failure/incomplete
terminals with valid usage are still charged, then raise a provider error. EOF
without completion, failures, and early closure without usage are uncertain.
Refusals, tool output, and unknown event/content types raise sanitized errors;
they are never silently represented as successful empty text.

Always consume to exhaustion or explicitly close the generator in `finally`:

```python
chunks = provider.stream(request)
try:
    for chunk in chunks:
        display(chunk.content)  # application UI; partial output is provisional
finally:
    chunks.close()  # may raise AccountingBlocked for an abandoned request
```

Cancellation is cooperative: `run.cancellation.cancel()` cannot forcibly stop
blocked network I/O. Checks occur before dispatch, between received events, and
after accounting. Streams close on cancellation, failure, completion, or explicit
generator closure. Keeping a suspended iterator alive without closing it retains
its reservation/socket; garbage-collection timing is not a cleanup guarantee.
Client `close()` releases provider-owned connections; injected clients remain
caller-owned. Transport cleanup failures cannot turn a failed operation into success.

## Privacy and trust limits

The provider reads `OPENAI_API_KEY` only when constructing its own SDK client.
Credentials and client objects are excluded from configuration representations.
The endpoint is pinned; environment proxies and redirects are disabled for owned
clients. Environment custom headers are rejected rather than transmitted;
organization/project/admin/webhook environment settings are not inherited.
`store=False`, `background=False`, `tools=[]`, and `service_tier="default"` are explicit.
`store=False` does not override the provider's service-side retention policies.

SDK/HTTP debug logs can include raw bodies, headers, and exception strings. On
construction this adapter suppresses the shared `openai`, `httpx`, and `httpcore`
logging namespaces and their existing children. This also affects other clients
using those loggers in the process. SOFIA/root logging settings are unchanged.
Do not re-enable third-party verbose logging around paid calls. Adapter errors
and logs contain no credentials, prompts, outputs, or raw upstream errors.

Injected SDK clients/transports and bound callables are trusted code: no hidden
network requests, retries, redirects, or sensitive logging may occur inside them.
The provider is an in-process contract, not a sandbox against arbitrary Python.

**A strict external billing guarantee is impossible.** A wrong/stale rate or
input bound, model/API changes, unknown usage, or provider overrun can exceed a
reservation before SOFIA detects it. Fail-closed settlement prevents subsequent
work but cannot undo incurred charges. Verify bounds/rates and set service-side
spending controls separately. No live API calls are part of automated validation.

Contracts checked against the installed official SDK 2.54.0 and the
[Responses request reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create),
[streaming event reference](https://developers.openai.com/api/reference/resources/responses/streaming-events),
[token-counting guide](https://developers.openai.com/api/docs/guides/token-counting),
and [SDK error/retry/timeout reference](https://developers.openai.com/api/reference/python)
on 2026-11-07. Run `python -m pytest -v`, Ruff, mypy, and `python -m pip check`
after installing `.[dev,openai]`. CI installs this extra on the existing
Windows/Python 3.11 and Ubuntu/Python 3.13 matrix.
