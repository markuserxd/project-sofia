# Typed tool registry

`sofia.tools` is offline infrastructure for trusted application controllers.
Nothing is registered automatically, and the CLI and providers do not invoke tools.
The registry does not parse model tool calls or implement an Agent Loop.

## Define, register, discover and execute

```python
from collections.abc import Mapping
from decimal import Decimal

from sofia.security import RunGuardrails
from sofia.tools import (
    JSONValue,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
)

run = RunGuardrails(max_steps=10, max_tokens=0, max_usd=Decimal(0))
tools = ToolRegistry(run=run)


def add(arguments: Mapping[str, JSONValue], context: ToolContext) -> JSONValue:
    context.checkpoint()
    a, b = arguments["a"], arguments["b"]
    assert type(a) is int and type(b) is int
    return {"sum": a + b}


tools.register(
    ToolDefinition(
        name="add",
        description="Add two integers in memory",
        parameters=(ToolParameter("a", "integer"), ToolParameter("b", "integer")),
        timeout=1.0,
    ),
    add,
)
assert tools.contains("add")
assert tools.describe("add").parameters[0].name == "a"
assert tools.list_tools()[0].name == "add"
result = tools.execute("add", {"a": 2, "b": 3})
assert result.succeeded and result.output == {"sum": 5}
```

`ToolDefinition` and `ToolParameter` are frozen dataclasses. Registration validates
them, rejects duplicates (`DuplicateTool`), and rejects malformed definitions,
handlers or security metadata (`InvalidToolDefinition`). Names match
`[A-Za-z][A-Za-z0-9_-]{0,63}`; descriptions contain 1–1024 characters, with some
non-whitespace text. Timeouts must be positive finite builtin integers or floats;
booleans are rejected. Handlers follow `ToolHandler(arguments, context)` and return
a JSON value synchronously. Handler signatures remain the trusted registrant's
responsibility; invocation errors become `handler_failed`.

Discovery returns a tuple of immutable public definitions or one exact definition.
Unknown lookup raises fixed-message `UnknownTool`; `contains` returns false.
Discovery has no effects and spends no steps. Handlers, closures, permission
objects, targets and application configuration are never returned. Public names
and descriptions must be safe, non-sensitive metadata supplied by trusted code.

## Argument and output subset

Arguments must be an exact builtin `dict` with string keys. Supported parameter
kinds are `string`, `integer`, `number`, `boolean`, `null`, `array` and `object`.
`number` accepts builtin integers and finite floats. `integer` accepts signed
64-bit builtin integers. Booleans never pass as numbers or integers. Optional
parameters use `required=False`; omission stays omitted, with no implicit default.
Null is allowed only for `null` parameters (or inside an array/object).

Arrays and objects contain the same JSON subset recursively. This is not JSON
Schema: nested field schemas, unions, defaults and coercion are unsupported.
Unknown arguments, missing required parameters, incorrect types, non-finite
numbers, tuples, arbitrary objects and builtin subclasses are rejected.
Strings (including object keys) are limited to 8192 characters; collections to
128 entries; nesting to eight edges from the root. Each payload has at most
4096 visited nodes (including object keys) and 65536 string characters in total.
Cycles exceed the depth bound. Outputs use these same limits.

Validation constructs new lists/dictionaries without invoking Python copying or
serialization hooks. Neither the caller's arguments nor handler-owned output
containers are shared with the result. Nested result containers are private
mutable copies; the result envelope is frozen. Callers must not concurrently
mutate an input during validation. Detected container mutation fails closed.
Handlers must return a stable output value, not a concurrently changing object.

## Execution and failures

Bind each registry to the task's existing `RunGuardrails`; share that same run
with providers and child operations. A future trusted Agent Loop controller can
call `list_tools`, decide on an invocation, and call `execute(name, arguments)`.
Keep the Python registry and its registration method out of model-accessible APIs.
Model output is data, never authority to register handlers or grant permissions.

Execution checks cancellation/accounting status, resolves the tool, copies and
validates arguments, checks the cooperative deadline, authorizes and consumes one
step, checks cancellation/deadline again, calls the handler, checks run state,
validates/copies output, and checks again before accepting success. Each attempt
has a fresh opaque `invocation_id`; there is no retry or deduplication facility.

`ToolResult` has `tool`, `invocation_id`, `output`, `error` and a computed
`succeeded` property. Success has `error=None`, including a valid null output.
Failures contain no fabricated output and have one of these fixed codes:
`unknown_tool`, `invalid_arguments`, `timeout`, `handler_failed`, `invalid_output`.
Unknown names use `tool=None`. Result repr omits output; serializing output is an
explicit controller decision and may reveal sensitive content to that controller.

Existing `GuardrailError` exceptions propagate unchanged, including permission,
step-limit, cancellation and accounting failures. Controllers must handle these
separately and stop a run where required. Exception text is never incorporated
into results or audit records. `KeyboardInterrupt`, `SystemExit` and other
process-level signals propagate and are audited as `interrupted`.

## Permissions and accounting

Only trusted registration code may designate a harmless in-memory tool by
omitting `action` and `target`. Such tools still call `run.budget.consume_step()`.
Protected tools register an existing `Action` and a fixed exact target:

```python
from sofia.security import Action

# A pure in-memory stand-in; this does not read a file or private data.
tools.register(
    ToolDefinition("protected_count", "Count an approved in-memory sample"),
    lambda arguments, context: {"count": 3},
    action=Action.SENSITIVE_READ,
    target="sample-record",
)
# Separate explicit controller approval, only after an actual user decision:
run.permissions.approve(Action.SENSITIVE_READ, "sample-record")
assert tools.execute("protected_count", {}).succeeded
```

The registry never approves permissions. Protected dispatch uses the existing
`run.begin_step(action, target)` and therefore the shared policy and atomic budget.
Wrong targets, unrelated actions and revoked approvals do not authorize execution.
Arguments cannot supply security metadata or approval. Targets must be non-empty
strings other than `*`; the policy uses exact equality, never wildcard matching.
There is no target resolver in this version: trusted controllers register a tool
for a fixed scope, and future argument-dependent capabilities must explicitly
design validated target resolution before introducing those tools.

`Action.API_REQUEST` is rejected at registration: provider calls must use
`run.begin_request()` and token/USD reservations. No paid HTTP tools are provided.
No new budget is constructed during invocation. Invalid/unknown calls and denied
authorization consume no step. Once dispatch consumes a step it is retained even
if the handler fails, times out or is cancelled. Concurrent invocations share the
existing atomic ledger and cannot exceed its step limit. Accounting-blocked runs
are rejected, including late results after a block becomes observable.

## Cooperative deadlines and emergency stop

`ToolContext.checkpoint()` uses the existing irreversible cancellation mechanism
and a monotonic deadline. Trusted handlers must checkpoint regularly, particularly
before further side effects, and bound any blocking work themselves. The deadline
starts after validation, before authorization. An injected trusted `clock` enables
deterministic tests. Cancellation is checked before execution and before accepting
output, including when a handler returns a late result without checkpointing.

Execution is synchronous in the caller's thread. No worker threads or background
operations are created. A checkpoint at/past the deadline raises `ToolTimeout`,
which becomes a fixed `timeout` result. A handler returning after its deadline
also fails. **This is cooperative timeout detection, not hard interruption.** A
blocked or non-cooperating handler can block the caller beyond the deadline; it
cannot safely be forcibly terminated here. Any side effects already performed
are not rolled back. Hard interruption requires future process isolation or
tool-specific cancellation support. The registry is not a sandbox for hostile
Python. Future file, desktop and browser handlers need their own path validation,
isolation, bounded I/O and cancellation contracts.

## Sanitized audit logging

Enable the existing `configure_logging(settings)` before controller work.
Every invocation attempt emits one final INFO event on `sofia.tools` when control
returns, including unknown tools, invalid input, permission/budget/accounting
denial, cancellation, timeout, handler failure, invalid output and interruption.
The existing JSON Lines formatter is unchanged: `message` contains a JSON object
with `event="tool_execution"`, opaque `invocation_id`, registered `tool` (or null),
fixed `status`, and `authorized`. Here `authorized` means dispatch passed its
permission/budget checks, including safe tools; it does not mean user approval was
granted. Existing `task_log_context` can supply an opaque task ID.

No arguments, outputs, permission targets, paths, URLs, raw unknown names,
exceptions or tracebacks enter the audit event. This is an allowlist, independent
of keyword redaction. Additional existing security events can accompany the final
tool event. A failing logging sink cannot change enforcement or execution outcomes;
as with existing security logging, persistence is best effort if that sink fails
or logging is disabled. A controller needing durable fail-closed auditing must
provide that policy separately. Trusted handlers must uphold these same privacy
rules in their own logs and public metadata.
