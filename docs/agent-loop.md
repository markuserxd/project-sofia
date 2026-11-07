# Bounded agent orchestration

`sofia.core` implements synchronous **Plan → Execute → Observe → Verify** over
the existing ToolRegistry. It is an orchestration layer, not unrestricted desktop
autonomy. No handlers, providers, credentials, approvals, or background work are
created implicitly. The CLI's behavior is unchanged. Only the standard library
and existing SOFIA contracts are used.

## Public interfaces and ownership

- `AgentTask(instruction, run, config=AgentConfig())` holds a bounded instruction,
  the existing trusted run, immutable limits, a generated opaque `task_id`, and a
  read-only `status`. IDs are correlation values, never permission or path authority.
- `AgentPlan(steps)` contains a tuple of `PlannedStep(tool, arguments)` objects.
  These are data envelopes, not executable code. Arguments use the registry's
  bounded JSON subset. The loop makes its own validated copies before dispatch.
- `Planner(instruction) -> AgentPlan` is trusted, local application code.
  `LLMPlanner(provider, run_guardrails)` is the explicit text-planning alternative.
- `Verifier(step, observation) -> VerificationResult` checks actual output against
  trusted expectations. `Observation.output` stays in memory and is excluded from
  repr and history. A verifier receives isolated copies of step arguments/output.
- `CompletionVerifier(records) -> VerificationResult` checks task-wide criteria
  using the immutable, sanitized attempt history. It is mandatory, as is the step
  verifier. It can check the required sequence/count of verified steps. Trusted
  applications needing additional evidence can retain it locally in their step
  verifier; never release it to a provider implicitly.
- `AgentLoop(registry=..., planner=..., verifier=..., completion_verifier=...,
  retry_tools=frozenset()).execute(task) -> AgentResult` claims one execution of
  that task. Concurrent or subsequent calls, even through another loop, raise
  fixed-message `ValueError("task_already_executed")` before work.

Planners, verifiers, registry registrations, guarded provider implementations,
and controllers are trusted Python. These interfaces are not an in-process
sandbox against malicious application code. Do not expose them, task objects,
or guardrail mutation to a model. Deterministic planners must remain local;
use `LLMPlanner` for provider calls so run identity is checked explicitly.

## Offline example

Run the separate demonstration from the repository root:

```powershell
python examples/agent_loop.py
```

It registers pure `add` and `multiply` handlers on the real registry and uses a
real shared run. The plan adds 2 and 3, then multiplies literal 5 by 4. Each output
must have exactly the expected shape, type, and value (5 then 20). Task verification
also requires the exact `add`, `multiply` sequence. There is no result expression
language. Demonstration modes print only safe status/counters:

```text
success: completed; verified=2; steps=2
failure: failed; verified=1; steps=2
cancel: cancelled; verified=1; steps=2
```

The failure mode raises inside the second handler. Cancellation occurs during the
second handler and preserves the first verified step. Each independent scenario
starts a separate task/run; no failed run is reset or resumed. All scenarios use
zero tokens and zero USD, and perform no network, filesystem, shell, browser, or
desktop actions. Printing the demonstration summary is the only application I/O.

A small standalone controller example:

```python
from decimal import Decimal
from sofia.core import (
    AgentLoop,
    AgentPlan,
    AgentTask,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.security import RunGuardrails
from sofia.tools import ToolDefinition, ToolRegistry

run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
tools = ToolRegistry(run=run)
tools.register(
    ToolDefinition("sample", "Return a fixed in-memory sample"),
    lambda arguments, context: {"count": 3},
)
loop = AgentLoop(
    registry=tools,
    planner=lambda instruction: AgentPlan((PlannedStep("sample", {}),)),
    verifier=lambda step, observation: VerificationResult(
        observation.output == {"count": 3}
    ),
    completion_verifier=lambda records: VerificationResult(
        tuple(record.tool for record in records) == ("sample",)
    ),
)
result = loop.execute(AgentTask("Verify the fixed sample count.", run))
assert result.status is TaskStatus.COMPLETED
assert result.completed_steps == result.budget.steps == 1
```

## States and phases

```text
CREATED → PLANNING → VALIDATING → RUNNING → OBSERVING → VERIFYING
                                  ↑                       |
                                  +----- next/retry -------+
                                                          |
                                        final verification → COMPLETED
```

Any active state can stop as `FAILED`, `CANCELLED`, or `LIMIT_EXCEEDED`.
`TaskState.transition()` rejects illegal edges and every transition out of a
terminal state. The final task verifier runs in `VERIFYING`. Each nonterminal
transition and the final task verification consume one orchestration iteration;
terminal transitions always remain possible after exhaustion. A two-step success
uses nine iterations. Transition history includes the initial and terminal state.

1. **Plan:** checkpoint the run, call the planner once, checkpoint again.
2. **Validate:** check the complete plan and isolate arguments. Unknown tools,
   malformed later steps, or oversized payloads reject the entire plan before
   any tool is invoked. Preflight does not approve an action.
3. **Execute:** check independent limits and the shared run, then call only
   `ToolRegistry.execute(name, arguments)`. The registry repeats validation,
   authorization, cancellation, accounting, and cooperative deadline checks.
4. **Observe:** inspect the actual `ToolResult` status. Reject failed calls and
   unsupported outputs before invoking a verifier. Record safe status separately
   from verification; an invocation returning successfully is not task success.
5. **Verify:** run the trusted step rule, recheck cancellation/accounting, and
   continue only on verified success or the narrow retry policy below. After all
   steps, run the final task rule and checkpoint before accepting completion.

Model claims, empty text, missing results, and reassuring observations never
override verification. Empty plans are rejected; there is no implicit no-op
completion. A trusted verifier may accept a legitimate null output only when
that is its explicitly configured criterion. There is no default permissive
verifier, automatic replanning, or hidden recovery/provider call.

## Strict text planning

The only format accepted is a JSON object with a nonempty `steps` array:

```json
{"steps":[{"tool":"sample","arguments":{}}]}
```

Top-level and step objects accept exactly the documented keys. Duplicate keys,
code fences, trailing text, non-finite JSON constants, missing fields, and extra
security/execution fields fail closed. No class names, import paths, callbacks,
expressions, tool registration, approval, provider choice, timeout, or budget
configuration are interpreted. String arguments are data subject to the tool's
schema; they never become executable code in this layer.

Instructions are limited to 8192 characters; accepted model text to 65536.
Plans have at most 128 steps (and the task's smaller limit applies). Typed plans
also have an aggregate serialized step-size cap of 65536 characters. Preflight
reuses `sofia.tools.schemas.validate_arguments`, including bounded depth, nodes,
collection sizes, strings, numeric ranges, and exact builtin types. It does not
implement a competing argument validator. The full plan remains privately owned
by the execution; mutation of original planner data cannot change later steps.

An explicitly configured mock can exercise the model path offline:

```python
from sofia.core import LLMPlanner
from sofia.providers import MockLLMProvider

# Reuse the tools/run from the standalone example. New task; new independent run
# here because that example already spent its one permitted step.
run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
tools = ToolRegistry(run=run)
tools.register(
    ToolDefinition("sample", "Fixed sample"), lambda arguments, context: {"count": 3}
)
planner = LLMPlanner(
    MockLLMProvider(response_text='{"steps":[{"tool":"sample","arguments":{}}]}'),
    run,
)
loop = AgentLoop(
    registry=tools,
    planner=planner,
    verifier=lambda step, observation: VerificationResult(
        observation.output == {"count": 3}
    ),
    completion_verifier=lambda records: VerificationResult(
        tuple(record.tool for record in records) == ("sample",)
    ),
)
assert (
    loop.execute(AgentTask("Use sample with no arguments; verify count 3.", run)).status
    is TaskStatus.COMPLETED
)
```

Ordinary `MockLLMProvider()` still returns `Hello from SOFIA`, which is **not** a
valid plan. Planning always has one attempt; malformed text fails without retry.
The controller must supply tool vocabulary in the instruction. Registry discovery
is available to trusted code but tool descriptions/outputs are not automatically
transmitted. Only the bounded instruction and a fixed format message reach the
configured provider. Selecting a paid planner explicitly releases that instruction
to that provider; verification and observations stay local.

## Shared permissions and accounting

The task and registry must reference the identical `RunGuardrails` object.
`ToolRegistry.run_guardrails` is a read-only identity property. Every non-mock
provider must implement the existing `GuardedProvider` contract and expose the
same run. Routed providers now expose their router's actual run through the same
read-only property, so `LLMPlanner(router.select("configured-route"), run)` works
without changing routing behavior or creating an independent fallback.

Paid adapters remain opt-in and own authorization, `begin_request()` reservations,
settlement, bounded request settings, and any previously configured transport
retry/fallback. The agent adds no request wrapper or extra planning call and
never constructs clients, reads credentials, or approves scopes. The mock consumes
no paid request step. Existing provider-internal attempts still charge their shared
budget, even though the agent has only one planner invocation.

The registry alone consumes the tool step; the agent never calls `consume_step()`
for the same invocation. A denied dispatch may count as an agent attempt without
consuming a run step. Each permitted retry calls the registry again and consumes
a fresh step. Run snapshots include all planning/provider/tool usage. Checks do
not reset or enlarge any limit. Once the run is accounting-blocked, even free
local work stops. Token/USD exhaustion is enforced by the paid adapter; a zero
token/USD budget still permits deterministic in-memory work.

## Finite limits and retries

All configured limits must be exact builtin integers; bool, float, negative,
unsupported, zero where inappropriate, and excessive values are rejected.

| Limit | Default | Allowed |
| --- | ---: | ---: |
| `max_plan_steps` | 16 | 1–128 |
| `max_attempts` | 32 | 1–1024 |
| `max_retries_per_step` | 0 | 0–3 |
| `max_iterations` | 128 | 1–4096 |
| `max_history` | 32 | 1–1024 |
| Planner invocations | 1 | Fixed at 1 |

Limits can be tighter than one another. The first applicable limit stops new work
and returns `LIMIT_EXCEEDED`; records are never silently discarded. One history
slot is reserved before each dispatch. Transitions are separately bounded by the
iteration cap plus initial/terminal entries. No scheduling/recovery loop can run
without consuming its applicable finite bound.

Retries are disabled by default. A retry is possible only after a **successful
registry result** fails deterministic verification, and only when all hold:

1. Trusted controller code explicitly names the tool in `retry_tools` and certifies
   it is a harmless, idempotent, in-memory operation with no billing or irreversible
   effects. An allowlist entry is a trust assertion, not inferred idempotency.
2. The trusted verifier returns `VerificationResult(False, retry_safe=True)` for
   a known safe condition, such as a deterministic readiness check returning false.
3. The per-step retry, total attempt, history, iteration, and shared run limits
   still permit another attempt, with cancellation/accounting rechecked.

No registry failure is retried: handler exceptions, timeouts, invalid outputs,
permission denial, guardrail failures, and ambiguous effects stop immediately.
Names such as `TemporaryError`, model text, or observation fields do not prove
safety. Approval is rechecked by the registry for every attempt. Exhausted retries
leave a verification failure, never success. Final task verification is performed
once and is never retried, regardless of its `retry_safe` field.

## Cancellation and failures

Cancellation is irreversible and checked before/after planning, before validation,
before/after tool dispatch, before/after verification, before retries, and before
final success. `OperationCancelled` becomes a `CANCELLED` result, never a recoverable
tool failure. A late provider/handler/verifier return cannot override a stop already
observed. Cancellation takes precedence if cancellation and accounting uncertainty
are both present; the budget snapshot still records the accounting block.

This is cooperative cancellation. A blocking Python callback or network operation
cannot be forcibly interrupted; callbacks must bound their own work and handlers
must checkpoint. There are no worker threads, hard timeouts, or rollback guarantees.
The loop bounds its scheduling, not arbitrary trusted callback wall-clock time.

Invalid task/configuration input raises a fixed-message `ValueError` before work.
Once admitted, ordinary failures produce exactly one `AgentResult` with a terminal
status. `FailureCode` distinguishes planning, malformed plan, unknown tool, invalid
arguments, tool failure, timeout, invalid output, verification, permission,
cancellation, each resource limit, provider failure, accounting uncertainty, and
run mismatch. Generic guardrail errors fail closed. No upstream exception message
is copied. Process-level signals such as `KeyboardInterrupt`/`SystemExit` propagate
after marking the task failed; they cannot leave a task eligible to restart.

## History, logging, and privacy

`AgentResult` contains the generated task ID, terminal status, verified step count,
tool attempt count, planner invocation count, iteration count, fixed failure code,
immutable history/transitions, and the existing immutable budget snapshot.
`StepRecord` includes a local numeric step ID, registered safe tool name, per-step
attempt, execution status, separate verification status, fixed failure code, current
phase, and tool invocation ID when returned. Registry guardrail exceptions do not
return their invocation ID; the agent records null and the registry retains its
existing tool audit event. An interrupted dispatch never invents a success result.

Successfully verified prior steps remain counted when later work fails. A step
interrupted by cancellation before acceptance is not counted as completed even
if its output or verifier had just returned. Task-wide verification can fail after
all individual steps passed; `status` remains the authoritative task outcome.

Agent INFO events use `sofia.agent`, separate from unchanged `sofia.tools` events:
`agent_started`, `agent_transition`, `agent_plan_validated`/`agent_plan_rejected`,
`agent_step_started`/`agent_step_finished`, `agent_verification`,
`agent_completion_verification`, `agent_retry_attempted`/`agent_retry_refused`, and
`agent_stopped`. Step-finished is emitted once per attempted dispatch, with separate
execution/verification statuses; stopped is emitted once for ordinary execution.
Enable the existing `configure_logging` to persist these events.

Only generated IDs, registered safe names, counters, enums, and fixed event codes
enter agent history/logs. No task instructions, raw plans, arguments, observations,
outputs, private targets/paths, credentials, upstream messages, or tracebacks are
included. Private envelopes suppress repr, but explicit application serialization
of private task/plan/observation data is still a disclosure: serialize `AgentResult`
for audits instead. Trusted metadata/callbacks must obey these same rules.
Broken logging handlers cannot change enforcement; persistence is best effort.
Durable fail-closed audit storage is outside this issue.

## Validation and future integration

The focused tests cover transitions, concurrent/recursive re-entry, strict JSON,
whole-plan preflight, output verification, finite limits, denied/ambiguous retries,
shared accounting, cancellation, injection-as-data, privacy, and the three offline
demonstrations. Existing test fixtures forbid live sockets. Paid provider integration
tests use real adapters over mocked transports, not live services.

Future desktop/file/browser handlers must supply their own validated paths, exact
permission scopes, effect-specific verification, bounded I/O, and cancellation.
They must not join the retry allowlist merely because they expose the same interface.
Persistence, scheduling, general data bindings, model-driven verification, recursive
planning, multi-agent work, and actual desktop autonomy are outside this issue.
