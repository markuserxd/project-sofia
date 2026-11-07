# Opt-in Anthropic text provider

Install `.[anthropic]` for application use or `.[dev,openai,anthropic]` for all
offline tests. The adapter targets official `anthropic>=2.6,<2.7` and its
`httpx2>=2.16,<2.17` transport. These are optional; ordinary package, mock, CLI,
accounting, and routing imports load neither provider SDK. `SOFIA_PROVIDER=mock`
and existing OpenAI settings remain unchanged. The CLI does not dispatch paid work.

Trusted application code explicitly constructs `AnthropicProvider` with a model,
local input bound, output cap, and the run's existing `RunGuardrails`. An owned
client reads only `ANTHROPIC_API_KEY` for authentication; it does not require an
OpenAI key. Injected SDK clients need no real credentials. Import and construction
make no network requests. A key alone never authorizes spending.

```python
from decimal import Decimal
from sofia.providers.anthropic_provider import AnthropicConfig, AnthropicProvider
from sofia.security import Action, RunGuardrails

run = RunGuardrails(max_steps=2, max_tokens=201_000, max_usd=Decimal("0.50"))
claude = AnthropicProvider(
    config=AnthropicConfig(
        model="claude-haiku-4-5-20251001",
        input_token_bound=lambda request: 200_000,
        max_output_tokens=256,
    ),
    run=run,
)
# Only the trusted controller, AFTER human approval of this exact paid scope:
# run.permissions.approve(Action.API_REQUEST, claude.permission_scope)
# response = claude.generate(request)
# Use response.content in the UI; never log it.
# Close the client in finally when finished:
claude.close()
```

The full documented context window is a deliberately conservative bound for
accepted input, including framing and every cache category. It requires reserving
the full window even for a short prompt. A smaller bound must be justified locally
by trusted code for that exact model and conversation; character counts are not
exact token counts. Missing, failing, non-integer, non-positive, or out-of-window
bounds reject before dispatch. No remote token-count request is made. As with the
OpenAI adapter, API rejection without reliable usage is treated as uncertain.

## Supported messages and streaming

`generate` and `stream` implement the unchanged synchronous `LLMProvider` protocol.
Leading system messages become separate ordered top-level `system` text blocks.
Conversation messages keep their roles, content, and order. Only alternating
user/assistant turns beginning and ending with user are supported. Late system
messages, adjacent same-role turns, system-only input, and assistant prefill are
rejected with sanitized `InvalidRequestError`, without moving or merging content.
This conservative subset avoids relying on differences in model placement rules.

Calls use `messages.create`, with `stream=True` for the documented raw SSE
interface. The adapter handles message start, ordered text block start/delta/stop,
cumulative message deltas, message stop, and ping. It preserves Unicode, empty
deltas, whitespace, and multiple text blocks. Input/cache counts are initialized
from message start, then replaced by applicable cumulative fields, never added.
Only a terminal message stop makes final usage authoritative. Partial usage is
never settled as a final bill. Final status must be `end_turn`; max-token stops,
refusals, missing completion/usage, malformed event ordering, tools, thinking,
citations, and unknown events/content are errors. An empty completed text result
is supported. Partial text is provisional until the iterator finishes successfully.

Streams close in `finally` on success, error, cancellation, or explicit consumer
closure. Consume to exhaustion or call `chunks.close()` in `finally`; abandoning
an iterator can retain its reservation and socket. Early close without final usage
raises `AccountingBlocked`. Cleanup failure never creates successful output or
masks an earlier failure. Injected clients are caller-owned; `provider.close()`
closes only provider-owned clients.

## Verified pricing and accounting

Supported first-party model identifiers and USD per million tokens, checked
2026-11-07 against the official model pages and pricing reference:

| Exact model | Input | Output | Cache read | 5m write | 1h write | Context / output cap |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `claude-haiku-4-5-20251001` | 1 | 5 | 0.10 | 1.25 | 2 | 200,000 / 64,000 |
| `claude-sonnet-4-6` | 3 | 15 | 0.30 | 3.75 | 6 | 1,000,000 / 128,000 |

Other models/aliases require an explicit code/pricing review and are rejected.
Only standard tier, standard speed, global first-party Messages billing is
supported. Requests explicitly use `service_tier="standard_only"` and disable
thinking. Priority, batch, fast mode, regional inference, partner platforms,
unrecognized duration/category fields, and server tool charges fail closed.
The adapter enables neither automatic caching nor cache breakpoints, but accounts
for all supported reported cache categories rather than assuming zero cache cost.

```text
total_input = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
total_tokens = total_input + output_tokens
cost = (input*input_rate + output*output_rate + reads*read_rate
        + writes_5m*write_5m_rate + writes_1h*write_1h_rate) / 1_000_000
```

`input_tokens` excludes cache reads/writes. The duration breakdown must add up to
cache creation, so creation is never counted twice. Counts must be non-negative
plain integers, excluding bool/float/string. Optional cache fields absent/null
mean inapplicable, consistent with SDK 2.6's contract; nonzero writes require a
complete duration breakdown. Unknown fields are preserved by SDK conversion and
rejected, including unknown zero-valued categories.

Prices are `Decimal`; exact rational arithmetic avoids ambient Decimal-context
rounding. Each attempt rounds upward once to micro-USD for ledger compatibility.
The reservation prices **all bounded input at the highest supported rate (1h
cache writes)** plus the full output cap. Actual settlement uses validated API
usage and its appropriate category rates, separately from that estimate.

Every attempt checks cancellation, obtains a local bound, computes the token/USD
reservation, and calls `run.begin_request("anthropic:<exact-model>", ...)`.
Permission and cancellation are checked again just before dispatch. No scope is
approved by the adapter. Settlement happens once even on failure/early closure.
Missing or unsupported usage commits the conservative estimate and permanently
blocks the shared ledger. Aggregate token/USD overruns and individual input-bound
or output-cap breaches record actual usage and block more work. The ledger's
additive `bounds_exceeded` settlement flag makes these sublimit checks atomic;
existing callers retain their default settlement behavior. Valid usage on an
unsuccessful completion is still charged.
Provider/cancellation errors retain their category with an `AccountingBlocked`
cause when settlement is uncertain. Security errors are never normalized into
ordinary provider errors. There is no second ledger or mutable last-usage state.

## Timeouts, fallback, privacy, and limits

Connect/read-write-pool timeouts default to 5/30 seconds and reject zero,
negative, boolean, invalid, NaN, or infinite settings. These bound socket
operations, not total response duration. Cancellation is cooperative and cannot
forcibly interrupt arbitrary blocked I/O. Checks run before dispatch, between
events, after blocking calls, and before returning. Completed validated usage is
retained when cancellation is observed after an SDK call.

SDK retries (normally two) are disabled, including on injected clients. The
adapter makes one attempt and never resumes streaming. Only an SDK connection
failure directly caused by transport `ConnectError`, `ConnectTimeout`, or
`PoolTimeout` during initial creation produces `UndispatchedProviderError`, after
settling known zero tokens/USD. It retains its consumed step. Generic failures,
read/write timeouts, HTTP errors, partial streams, or unknown billing never
produce that evidence. See [routing](model-routing.md) for the explicit fallback
subset; the existing OpenAI adapter's terminal temporary errors remain ineligible.

Owned clients pin `https://api.anthropic.com`, disable environment proxies and
redirects, reject environment custom headers, and pass explicit API-key
credentials to prevent profile/federation discovery. SDK OpenTelemetry tracing
is disabled. Context-local source-logger filters suppress SDK/HTTP transport
records during each SDK operation without disabling unrelated clients/threads
or logging while an iterator is suspended. The filters remain installed but are
inert outside these calls; they cover the supported SDK/transport logger families.
Do not install hooks/middleware that log payloads or secretly perform requests.
Injected clients, transports, token bounds, and route mappings are trusted
application code, not a sandbox against arbitrary Python.

SOFIA logs only fixed operational messages and numeric token/category/rounded
USD metadata; logging failures do not alter accounting. Exceptions do not contain
upstream strings, HTTP bodies, prompts, outputs, or credentials. Existing JSON
logging/redaction remains unchanged. Service-side retention policies still apply.

Local reservations cannot guarantee an external invoice ceiling: stale rates,
incorrect bounds, API changes, or provider overruns may incur charges before
SOFIA can stop subsequent work. Use provider-side spending controls separately.
No tools, images, audio, extended thinking, caching activation, background jobs,
agent loop, or automatic discovery are implemented. Validation uses real SDK
parsing over mocked transports; **no live paid API calls were tested**.

Official sources: [Python SDK](https://platform.claude.com/docs/en/api/sdks/python),
[Messages API](https://platform.claude.com/docs/en/api/python/messages/create),
[streaming](https://platform.claude.com/docs/en/build-with-claude/streaming),
[usage/cache semantics](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[pricing](https://platform.claude.com/docs/en/about-claude/pricing),
[Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview), and
[Sonnet 4.6](https://platform.claude.com/docs/en/models/sonnet-4-6/overview).
