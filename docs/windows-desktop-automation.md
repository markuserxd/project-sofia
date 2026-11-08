# Controlled Windows desktop automation

Issue #11 adds an opt-in foundation, not general Windows application control.
The CLI does not instantiate a desktop adapter, import native dependencies,
register desktop tools, launch applications, or automate input at startup.

## Installation and platform

From the repository root in an ordinary, non-administrator terminal:

```powershell
python -m pip install -e ".[dev,desktop,openai,anthropic]"
```

The desktop extra pins [pywinauto 0.6.9](https://pypi.org/project/pywinauto/)
and requires pywin32 >=311, with Windows-only environment markers. Its transitive
dependencies include comtypes and six. Real integration was exercised on this
development host with Windows, CPython 3.14.3, pywinauto 0.6.9, pywin32 312,
and comtypes 1.4.17. Offline CI targets Windows/Python 3.11 and Ubuntu/Python 3.13;
those jobs do not install desktop packages or require a GUI. A successful offline
CI run does not establish native compatibility on every Python/Windows version.

The native backend targets Windows 10/11 UI Automation in an active, unlocked,
interactive WinSta0/Default session at medium integrity. It rejects Session 0,
disconnected/unavailable sessions, inaccessible input desktops, non-default
desktops, elevated controllers/targets, other users and other sessions. It never
switches desktops, elevates, bypasses UIPI, or installs global hooks. Importing
the modules is safe on other platforms; real invocation returns
`unsupported_platform`, never simulated success. Missing extras fail closed.

## Components and trust boundaries

* `sofia.tools.desktop`: immutable configuration/proposals, bounded reference
  stores, exact existing permission scopes, registry handlers, verification,
  cancellation and sanitized audit events.
* `sofia.tools.desktop_backend`: small protocol, immutable window/control records,
  trusted application/control definitions and executable fingerprints.
* `sofia.tools.windows_desktop`: lazily imported native implementation using
  pywin32 process/session APIs and pywinauto's UIA wrappers/interfaces.
* `examples/desktop_fixture.py`: standard-library ctypes GUI with native Edit,
  Button and Static controls and synthetic data. These provide more reliable UIA
  Value/Invoke patterns than Tk widgets. It performs no file, network, clipboard,
  credential, browser or system-settings operations.

Trusted Python controllers configure applications, register tools, inspect
proposals, obtain actual user decisions and grant/revoke existing policy scopes.
Neither Python configuration nor these controller functions are model tools.
These interfaces are not a sandbox against malicious trusted Python code.

## Allowlisting and launch identity

`DesktopConfig()` has an empty immutable application tuple. Each
`DesktopApplication(app_id, executable, window_class, controls=(), arguments=(),
multiple_instances=False)` requires a trusted exact absolute `.exe` path, exact
window class, fixed arguments and explicitly assessed benign controls. Relative,
missing, empty, oversized, symlink/reparse paths (including ancestor junctions),
duplicate IDs/executables/selectors and common interpreters, shells, browsers,
administrative, installer and credential-tool names are rejected. No executable
lookup on PATH, shell strings, executable selectors, URL handlers or model
supplied arguments are accepted.

The controller must assess the binary and each permitted control's consequences.
The filename denylist is defense in depth: renaming a malicious or sensitive
application does not make it safe. Never allowlist banking, wallets, credentials,
password managers, security settings, browsers or administrative applications.
Fixed launch arguments must also be harmless. Do not run from an untrusted
directory, environment, or dependency search path.

Configuration fingerprints the canonical file ID, size, modification time and
SHA-256 content (maximum 128 MiB). Each operation rechecks that fingerprint.
Launch also holds read-only Windows file handles denying write/delete sharing
while revalidating and creating the process. `Popen` receives a list, exact path,
fixed cwd, `shell=False`, closed inherited handles and null standard streams.
There is no elevation request. Process creation is followed by bounded readiness
checks matching the returned PID, creation time, exact executable and window class;
multiple matching windows fail as ambiguous. An existing matching window or
accessible process blocks launch when multiple instances are disabled. Native
process scanning is capped at 4096 PIDs; inaccessible/exited unrelated processes
disclose nothing. This check is not an atomic OS-wide single-instance lock.

Path/content fingerprints are not publisher signatures or code attestation.
They do not attest DLLs/plugins, prevent every ancestor-directory race, or prove
that an already-running process loaded the current on-disk bytes. Use only stable,
trusted local installations; shared/hostile-writer installations are unsupported.
The adapter does not adopt transferred/singleton-process launches: if the expected
PID never owns a ready window, launch fails instead of guessing a replacement.

`fixture_application()` is a repository-only exception for the exact base Python
interpreter, `-I`, and the fixed synthetic fixture script, also fingerprinted and
pinned. Using the base interpreter avoids Windows venv redirector child PIDs.
Only Python child processes actually created and retained by that backend can
participate. Other Python windows are rejected; general-purpose scripts cannot
be supplied to this factory or to tools.

## Tools, permissions and previews

```python
from decimal import Decimal
from sofia.security import Action, RunGuardrails
from sofia.tools import DesktopAutomation, DesktopConfig, ToolRegistry
from sofia.tools.desktop_backend import fixture_application

run = RunGuardrails(max_steps=30, max_tokens=0, max_usd=Decimal(0))
app = fixture_application()  # synthetic repository demo only
desktop = DesktopAutomation(DesktopConfig((app,)), run=run)
registry = ToolRegistry(run=run)
desktop.register(registry)

# Trusted controller, after obtaining the user's observation permission:
run.permissions.approve(Action.DESKTOP_OBSERVE, desktop.permission_scope)
run.permissions.approve(
    Action.DESKTOP_OBSERVE, desktop.target_scope(app.app_id, "observe")
)
result = registry.execute(
    "desktop_preview",
    {
        "app_id": app.app_id,
        "operation": "launch",
    },
)
# Check result.succeeded AND result.output['status'] before continuing.
```

Registration requires object identity with the registry's existing run. The
default prefix is `desktop`; registration is explicit and cannot be repeated.

| Tool | Arguments | Registry capability |
| --- | --- | --- |
| `desktop_discover` | `app_id` | `DESKTOP_OBSERVE` |
| `desktop_inspect` | `app_id`, `window_id` | `DESKTOP_OBSERVE` |
| `desktop_preview` | `app_id`, `operation`, optional `window_id`, `control`, `text` | `DESKTOP_OBSERVE` |
| `desktop_launch` | `preview_id` only | `APP_LAUNCH` |
| `desktop_interact` | `preview_id` only | `UI_INTERACT` |

All five tools register their action with a generated, run-local adapter scope.
Handlers additionally check exact existing `PermissionPolicy` targets; no second
authorization policy is introduced. Observation requires
`target_scope(app_id, "observe")`. Mutations require the capability at the adapter
scope, the exact operation/window/control target scope, and the unique preview
scope. Preview creation only observes; it grants nothing and performs no input.

To approve a concrete proposal after **a real user decision**, trusted code does:

```python
proposal = desktop.proposal(preview_id)  # local private review, includes exact text
action = Action.APP_LAUNCH if proposal.operation == "launch" else Action.UI_INTERACT
run.permissions.approve(action, desktop.permission_scope)
run.permissions.approve(
    action,
    desktop.target_scope(
        proposal.app_id,
        proposal.operation,
        window_id=proposal.window_id,
        control=proposal.control_key,
    ),
)
run.permissions.approve(action, desktop.approval_scope(preview_id))
# Then invoke desktop_launch or desktop_interact through registry.execute.
```

These approval calls are not an automatic approval recipe for an AI planner.
A controller must show the actual proposal and obtain the user's decision first.
The demo's synthetic test controller is explicitly labeled as such.

The immutable proposal binds its opaque ID to the exact application, window and
process snapshot, control runtime identity, operation, private text and digest.
Public preview output contains safe IDs, operation, control key and text length;
it contains no typed content, UI values, titles or executable paths. Execution
accepts no replacement text/target, `approved` flag, coordinates or hotkeys.
Approvals are consumed and revoked before native work, including failed attempts.
Expired previews (default 60s), wrong operation tools, forged IDs and reused IDs
fail closed. `discard()` revokes pending authority without performing UI work.
Keep the adapter and its pending private text local; discard proposals when done.

The registry alone consumes one shared execution step per dispatched tool.
Handlers call `permissions.check`, not `begin_step`, so there is no double charge.
There is no budget reset, additional ledger, hidden retry or model approval tool.

## Discovery, targeting and operations

Discovery reads only allowlisted application metadata and never focuses a window.
It does not read window titles or arbitrary control values. It returns opaque
window IDs and visible/enabled/minimized flags. Inspection returns only configured
control keys, types, safe enabled/visible state and a foreground boolean. All raw
native identity records remain local. Same-title windows remain distinct targets.

Window checks bind executable path, PID, process creation time, HWND, exact window
class and UIA runtime ID. The backend retains the original UIA provider object;
stale/unavailable providers and changed identities fail closed. It also verifies
same-user/session/medium-integrity ownership and the Default desktop. Before
interaction the adapter rechecks the target and the backend repeats identity,
control and focus checks immediately before invoking the semantic interface.

Only direct-child selectors from trusted `ControlRule` definitions are supported:
exact Automation ID plus control type, scoped to the approved window and PID.
Zero or multiple matches, changed runtime IDs, disabled/invisible/password fields,
read-only edits and unsupported patterns are rejected. No descendant recursion,
arbitrary property chains, selectors, screenshots or UI trees are exposed.

* **Focus:** `SetForegroundWindow` only, followed by foreground verification.
  Foreground-lock refusal is `focus_mismatch`; no injected Alt key or attachment
  of input queues is used to bypass Windows focus restrictions.
* **Text:** exact configured Edit control's UIA Value.SetValue, as literal text;
  private Value comparison verifies the expected synthetic value. Default maximum
  length is 256 characters (configurable up to 1024); all characters must be
  printable. Empty input, control characters, secure/password/read-only edits and
  unknown control types are rejected. Literal shortcut-like strings are never
  interpreted as keystrokes. Secret detection in arbitrary text is impossible:
  controllers must allow only non-sensitive text and approved benign controls.
* **Click:** a single UIA Invoke on an explicitly configured benign Button, with
  an exact configured synthetic Text-status ID and expected postcondition.
  Verification returns a boolean, never the status text. A button whose semantic
  consequences are unknown must not be configured.

The backend uses pywinauto for UIA identification and interfaces. It deliberately
avoids its higher-level text setter (which logs payloads) and invoke helper (which
can create a worker thread). [UIA pattern documentation](https://pywinauto.readthedocs.io/en/latest/code/pywinauto.controls.uia_controls.html)
describes the supported semantic interfaces. There is **no PyAutoGUI fallback**:
raw coordinates, DPI/multi-monitor assumptions, global input, keyboard shortcuts,
clipboard transport and fail-safe overrides are unsupported. Multi-monitor/DPI
geometry is therefore never used to target input.

## Limits, stop and results

Default bounds: 16 configured applications, 16 returned windows (maximum 32),
1024 top-level native handles scanned, 4096 process IDs checked before launch,
64 direct controls per inspected window,
64 remembered windows/provider objects, 32 pending previews (maximum 64), 5s tool
deadline (maximum 30s), and 40 readiness attempts with 50ms intervals (maximum 100).
Exceeding discovery/reference/tree limits fails closed instead of choosing a
partial arbitrary target. Blocking native calls are synchronous and checked
before and after. Readiness uses the registry's monotonic deadline and a separate
attempt cap. Configure registry and adapter clocks consistently in synthetic tests.

Call `run.cancellation.cancel()` from the trusted controller for an irreversible
emergency stop. Checkpoints guard discovery, previews, approval validation, native
launch, readiness polling, focus/input, verification and result acceptance.
Accounting-blocked runs also prevent further side effects. No worker continues
input after return, and no action is automatically retried. Cancellation cannot
undo a completed click, text change or launch. A launched process may remain
running after cancellation/timeout; production code does not automatically kill it.

Deadlines and stop are cooperative. A blocked OS/COM/UIA provider can exceed its
deadline and cannot safely be forcibly interrupted by this adapter. This version
does not provide native process isolation or a hard timeout guarantee. Concurrent
desktop changes, malicious providers and same-process handle/runtime-ID reuse
cannot be defeated in every race between validation and invocation. Avoid hostile
applications and concurrent operators. Changed/unverifiable targets fail closed;
there is no claim of atomic UI targeting or unrestricted production readiness.

Successful desktop output has `status="ok"`; mutations also have `verified=True`.
Application-level failures have `status="error"` and a fixed code such as
`target_not_found`, `ambiguous_target`, `target_mismatch`, `unsafe_target`,
`unsupported_session`, `focus_mismatch`, `launch_failed`, `timeout`,
`stale_preview`, `preview_not_found`, or `verification_failed`. Unknown exception
messages are replaced with `desktop_failed`. Registry deadline failures still
use `ToolResult.error="timeout"`; existing permission/cancellation/budget errors
propagate under the unchanged registry contract. A mutation may already have had
an effect when verification or a late checkpoint fails: do not retry it blindly.

## Agent Loop and audit

Use the **same** run for adapter, registry, AgentTask and optional planner. Keep
desktop tools out of `retry_tools`. The existing AgentLoop dispatches registered
tools and calls trusted verifiers; inspect `output['status']` and
`output['verified']` separately from `ToolResult.succeeded`. A Python handler
returning an application error is still a successful dispatch. The demo uses
the real AgentLoop for approved focus/text/click, real backend postcondition
checks, a step verifier and final completion verifier. No model-generated scripts
or model claims of success are used. Offline integration tests cover failed
verification, denial, exact step accounting and no automatic retries.

Enable existing SOFIA logging in the controller. `sofia.desktop` emits fixed
discovery, preview, denial, action-start/completion, rejection, timeout and
cancellation events. Rejection codes distinguish identity/session/unsafe-target
failures. Metadata is an allowlist of fixed event/code values. The existing
registry invocation IDs and optional task logging context provide correlation.
No titles, typed text/digests, paths, exception strings, screenshots, clipboard,
control values or raw accessibility trees are audited. Proposals and process
records omit sensitive fields from repr. Logging persistence is best effort;
logging failure cannot authorize anything. A cancellation before handler dispatch
is visible through existing security/registry events.

## Tests and interactive demonstration

Offline tests require no desktop dependencies, user applications or paid APIs:

```powershell
python -m pytest tests/test_desktop_security.py tests/test_desktop_launch.py tests/test_desktop_backend.py tests/test_desktop_integration.py -v
```

Run the dedicated fixture demonstration in an unlocked, non-administrator Windows
session. The default mode asks for each exact action decision in the terminal:

```powershell
python examples/desktop_demo.py --run-gui
```

For the **synthetic test controller**, whose approvals do not represent real user
decisions:

```powershell
python examples/desktop_demo.py --run-gui --synthetic-approvals --wait-for-focus 20
$env:SOFIA_DESKTOP_SMOKE = '1'
python -m pytest tests/test_desktop_integration.py::test_real_windows_fixture -v
Remove-Item Env:SOFIA_DESKTOP_SMOKE
```

When requested, bring only the synthetic fixture to the foreground within the
bounded observation interval. The `--wait-for-focus` option accepts 0–60 seconds;
it polls read-only registered inspection tools, never retries a mutation or
bypasses OS focus locks. Each observation consumes a shared step. A remote/headless
CI session may be unsuitable even if it reports Windows: run the GUI test locally.
Ordinary CI skips this test explicitly and never sends real keyboard/mouse input.

The demo first rejects unapproved launch, launches its exact fixture, discovers
and validates it, rejects an unrelated window ID, previews/approves/verifies
focus, synthetic text and one button invocation, then proves cancellation stops
another approved action. `finally` terminates only actual retained child process
handles and waits at most 5s per child. It never kills a process by an arbitrary
PID or executable name. No real working application is involved.

On the development host the full standalone synthetic GUI scenario passed.
Immediate foreground activation initially failed closed because of Windows
foreground restrictions; waiting for the fixture to become active allowed the
entire scenario to pass. This is evidence for the fixture/session only, not a
claim of general application compatibility. Re-run after changing the backend,
Windows version, UIA provider, Python, dependencies or session environment.

Local verification on 2026-11-08 (Windows / Python 3.14.3):

| Check | Result |
| --- | --- |
| Full `python -m pytest -v --basetemp=.tmp/pytest-complete` | 974 passed, 3 expected skips |
| Focused four desktop test modules | 102 passed, 1 opt-in GUI skip |
| Opt-in `SOFIA_DESKTOP_SMOKE=1` GUI pytest | 1 passed, synthetic approvals |
| Standalone GUI demo with bounded foreground wait | Passed; synthetic approvals, operator activation also exercised |
| Ruff lint and format check | Passed |
| mypy, Windows and Linux target platforms | Passed, 36 source files |
| CLI help/version/default and `pip check` | Passed |

The sandbox prevented existing workspace tests from accessing native volume APIs;
the complete passing regression run used normal local Windows filesystem access.
An unattended GUI retry correctly failed with `fixture_focus_unavailable` when
Windows withheld foreground activation. The opt-in test is operator-assisted when
necessary and must not be interpreted as a guaranteed unattended desktop workflow.
