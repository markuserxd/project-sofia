# Per-run security guardrails

`sofia.security` contains offline enforcement primitives. It makes no network
calls, reads no credentials, and executes no tools. CLI, configuration, and
provider interfaces are unchanged. Limits are explicit constructor arguments.

## Ownership and approval

Trusted application code creates **one `RunGuardrails` per run** and passes the
same object to every child operation. Never construct a fresh budget for each
retry, stream chunk, or child task. Context components are read-only properties;
there is no reset API. Separate runs have independent state.

All `Action` members require approval: file writes/deletion, shell execution,
software installation, sending messages, submitting forms, changing settings,
sensitive reads, and API requests. Unknown actions (including raw strings) are
rejected. Approval is membership in an application-owned policy set, scoped to
the exact `(Action, target)` pair. `approve()` explicitly sets that status;
`revoke()` removes it immediately for subsequent checks. Requests have no
`approved` field. Missing/blank targets and the unrestricted `*` target are
invalid. No case folding, path normalization, pattern expansion, or mutation of
targets occurs; other strings containing `*` are literal resources only.

Only a trusted controller, after obtaining human approval, may call `approve()`.
Never expose approval methods, budget construction, settlement, or Python object
access as model tools. A model's JSON/text claims are not authority. These are
in-process enforcement contracts, **not a sandbox against arbitrary Python code**.
Future tools must validate their own paths and arguments and bind approval to
the full operation (including command/content/options where relevant), not just
a filename. This issue deliberately does not implement that tool boundary.

## Usage with the existing offline mock

```python
from decimal import Decimal

from sofia.providers import LLMMessage, LLMRequest, MockLLMProvider
from sofia.security import AccountingBlocked, Action, RunGuardrails

run = RunGuardrails(max_steps=5, max_tokens=100, max_usd=Decimal("0.10"))
# Trusted controller only, after explicit approval of this exact scope:
run.permissions.approve(Action.API_REQUEST, "offline-mock")
request = LLMRequest((LLMMessage("user", "Hello"),))
reservation = run.begin_request(
    "offline-mock", estimated_tokens=20, estimated_usd=Decimal("0")
)
try:
    run.cancellation.checkpoint()
    response = MockLLMProvider().generate(request)
except Exception:
    # Unknown usage is charged at the estimate and blocks further work.
    # Catch the accounting signal here to preserve the original failure.
    try:
        run.budget.settle(reservation, actual_tokens=None, actual_usd=None)
    except AccountingBlocked:
        pass
    raise
else:
    # Example fixture accounting, NOT tokenization or model-reported text.
    run.budget.settle(reservation, actual_tokens=10, actual_usd=Decimal("0"))
    run.cancellation.checkpoint()
```

For a non-API action, call `run.begin_step(action, target)` before its effect.
API actions must use `begin_request`; `begin_step` rejects `Action.API_REQUEST`.
The unified methods check cancellation, permission, and limits. Lower-level
`PermissionPolicy`, `Cancellation`, and `RunBudget` are available for trusted
integrations, but do not independently enforce each other's responsibilities.

## Accounting rules

- Counts must be non-negative plain integers; booleans and floats are rejected.
  Zero limits are valid. Each successful begin consumes exactly one step, which
  is never refunded, even on failure. A rejected begin consumes nothing.
- Request preflight atomically reserves tokens, spending, and a step under one
  lock. Competing operations cannot reserve the same capacity. Missing/invalid
  estimates never fall back to an unbounded budget or a free request. Explicit
  zero estimates are allowed only when the trusted adapter can justify them.
- Money is USD, provided as finite non-negative `Decimal` values exactly
  representable in **micro-USD (one millionth of a dollar)**. Finer amounts are
  rejected, never rounded down. Trusted pricing adapters must round estimates
  upward to this unit. Storage uses integers, so arithmetic is exact regardless
  of the caller's Decimal context. Binary floats are never accepted.
- A `Reservation` is an opaque identity handle belonging to its original ledger.
  Fabricated, copied, foreign, and already-closed handles cannot settle. There is
  no general release method that could accidentally refund another reservation.
- `settle` validates both actual values before mutating anything. Valid trusted
  usage replaces reserved amounts with committed usage; unused capacity becomes
  available. A trusted adapter may report zero only when it knows usage/charge
  was zero (for example, cancellation before dispatch). Consumed usage is never
  refunded. An invalid settlement leaves the reservation open for correction.
- `None` means usage or cost is unknown: commit that resource's entire estimate,
  close the reservation, latch the run blocked, and raise `AccountingBlocked`.
  A failed/cancelled request is not presumed free. An unclosed reservation stays
  reserved; exceptions and garbage collection never automatically release it.
- Actual usage above either estimate is fully recorded, even above the run cap;
  settlement closes the handle and raises `AccountingBlocked`. Both overruns
  and unknowns permanently deny all subsequent steps/reservations, including
  zero-cost work. Already-open reservations may still settle. There is no reset
  or retroactive refund API; trusted controllers must surface this run failure,
  not silently create another run to continue the same task.
- Adapters may pass `bounds_exceeded=True` at settlement when a validated
  provider-specific input/output limit was breached despite spare aggregate
  capacity. This records actual tokens/USD and blocks atomically, using the same
  overrun rules. The optional flag defaults to false for existing integrations.
- Snapshots are immutable and contain only counters. Remaining capacity is
  clamped at zero after an overrun; committed totals retain the full actual cost.
  Cancellation/blocked status still takes precedence over apparent capacity.

Reservations alone cannot bound an external provider's bill. Real adapters must
use validated pricing, explicit output-token caps, trusted API usage, bounded
timeouts, and conservative settlement. Never derive actual usage from model
text. Overrun detection cannot undo an already-incurred external charge.

## Emergency stop and checkpoints

The trusted controller calls `run.cancellation.cancel()`. A `threading.Event`
makes stop observable across threads. Repeated calls are safe and no reset is
provided. Stop takes priority over authorization, invalid input, and budget
denials for new operations. Accounting cleanup and revocation remain possible
after stop; neither can authorize new work.

Call `checkpoint()` before each operation, before dispatch/high-impact effects,
between streaming chunks, and immediately after blocking calls return before
using their result. Recheck permission immediately before delayed effects too:
an earlier check is not a transferable or permanent execution authorization.
Cancellation and revocation do not atomically wrap arbitrary external effects.
Work already admitted may run until its next cooperative checkpoint.

Python cannot forcibly interrupt arbitrary blocked I/O, remote requests,
subprocesses, or third-party code through this flag. Future adapters need their
own timeouts, interruption handling, and safe process termination where relevant.
No unsafe thread termination or background service is implemented here.

## Errors and privacy

Failures use small `GuardrailError` subclasses with fixed, display-safe reason
codes, including `PermissionDenied`, `UnknownAction`, `OperationCancelled`, limit
errors, and accounting/input errors. Logs use the existing `sofia.security`
namespace and contain constant reason codes only. Targets, arguments, prompts,
responses, and exception payloads are never included. Logging failures do not
disable enforcement. Applications can use the existing opaque task log context.
