# SOFIA desktop UI

Issue #15 adds an optional, offline PySide6 Widgets application. It is a working
chat and bounded task demonstration, with real Agent Loop execution and human
approval controls. It does not enable general autonomous desktop operations.

## Install and launch

Use an ordinary Windows account, without administrator privileges:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ui,browser,openai,anthropic]"
.\.venv\Scripts\python.exe -m sofia.ui
# Alternatives after installation:
.\.venv\Scripts\sofia-gui.exe
.\.venv\Scripts\python.exe examples/ui_demo.py
```

For GUI use alone, `python -m pip install -e ".[ui]"` is sufficient. API keys,
internet, Playwright binaries, native desktop extras and an environment file are
unnecessary at runtime. Startup validates the existing Settings contract: provider
must be `mock`. No work starts until a user submits a message or clicks a demo.
Missing Qt produces a short installation instruction and exit code 2.

The optional range `PySide6>=6.10.2,<6.11` supports Python 3.11, 3.13 and 3.14.
[PyPI's 6.10.2 metadata](https://pypi.org/project/PySide6/6.10.2/) specifies Python
`>=3.9,<3.15`, with stable-ABI Windows and Linux wheels. Local verification uses
Windows / Python 3.14.3 / PySide6 6.10.3. The full PySide6 distribution includes
Essentials, Addons and Shiboken; the GUI uses Core, Gui and Widgets, and tests use
QtTest. No additional UI framework or pytest-qt dependency is installed. On Linux,
native display use requires Qt's platform libraries; offscreen CI needs no display.
Ubuntu also needs `libegl1` and `libopengl0` even for QtGui/QtTest imports without
a display. CI installs those runtime libraries explicitly. For a local Ubuntu
installation, install them with the system package manager before running Qt.

The original `python -m sofia`, `--help`, `--version` and `sofia` CLI remain
headless. Importing `sofia` or `sofia.ui` does not import Qt or native tools.

## Interaction and layout

The resizable window has a recent-run sidebar, scrollable conversation, task
details, a composer, demo buttons, Settings, Cancel and a prominent Emergency Stop.
The minimum size is 840 × 600 logical pixels; Qt handles system DPI scaling.
Panels can be resized using their splitters. Keyboard navigation and accessible
names cover the main controls. System fonts and Qt fallback support Thai, English,
mixed Unicode, emoji and multiline text; no font files are bundled.

Enter sends; Shift+Enter inserts a newline. Blank input is rejected; input is
limited to 4,096 Unicode characters. During a run, submission and demo controls
are disabled, with a second controller check preventing duplicate starts. Settings
and Emergency Stop remain accessible. New chat clears the visible transcript.
Only the latest 80 message bubbles and 20 run outcomes are retained in memory.

Chat uses the existing MockLLMProvider with a fixed friendly bilingual reply. It
does not understand arbitrary requests, plan from conversation text, or activate
tools mentioned in messages. OpenAI/Anthropic are shown as unavailable. There is
no API-key field and no real-provider onboarding in this issue.

All conversation, status and review details are plain text. QLabel rich text and
external links are disabled; previews use read-only QPlainTextEdit. HTML, Markdown,
URLs, local file references and images have no active rendering behavior. Fixed
application-owned styles are separate from all input and provider content.

## Controller, threads and progress

`ui/controller.py` owns a fresh AgentTask and RunGuardrails for each submission,
with 8 steps, 0 API tokens and USD 0. It owns one QObject worker in one QThread.
The worker calls synchronous core APIs. UI updates arrive through queued signals
carrying frozen bounded dataclasses. Widgets never run AgentLoop.execute, update
from worker threads, or access private adapter mutation methods.

Run safe task registers an in-memory mock tool through the existing ToolRegistry,
runs a fixed three-step AgentPlan in the actual AgentLoop, and uses deterministic
step and completion verifiers. Run slow task uses the same plan with bounded,
checkpointed worker delays to make cancellation observable. No model decides
which tools to register, which plan to execute or whether verification passed.

The GUI initially displays the real CREATED state. The trusted planner reports
PLANNING, the dispatched handler reports RUNNING, and the trusted verifier reports
VERIFYING, reading the actual task status at each callback. There is no core API
change or fabricated live transition stream. VALIDATING and OBSERVING may be too
brief to observe live; the final view includes every actual AgentResult.transition
and StepRecord, attempts, completed steps, verification and fixed failure codes.
It displays the actual budget ledger, including committed/reserved tokens and USD.
Zero usage means no paid call occurred, not an invented token estimate.

Unexpected worker exceptions are replaced with `worker_failed`. Diagnostics use
the existing sanitized logger and a fixed message, never raw exceptions. Unknown
tools, invalid plans/arguments, planning/provider errors, permission denials,
verification failures, accounting blocks and exhausted limits use core failure
codes. No tool outputs or private observations are copied into the task view.

## Human approval and exact binding

Review protected demo creates one immutable, synthetic proposal in the worker's
trusted planner, before any protected dispatch. The dedicated fixture changes
only a disposable memory cell; FILE_WRITE is its protected action category, and
**no actual file is written**. Its scope combines task identity, adapter identity,
preview identity and a SHA-256 binding to the exact synthetic content. The existing
PermissionPolicy additionally binds the action. The review and execution share
the identical immutable proposal; UI-created replacement identities are rejected.

The nonmodal review window shows the adapter, run, instance, preview, action,
target, exact content, consequence, reversibility and expiration. Deny is the
default button; Approve has no default Enter shortcut. Dismissal and Escape deny.
The user can still click Emergency Stop in the main window. Only one approval
can be pending, and proposals expire after 60 seconds.

The worker waits on a Condition in short bounded intervals, not a Qt nested event
loop. A dialog click passes the exact review identity to the trusted controller.
Displaying a dialog grants nothing. The decision only wakes the worker; the
worker revalidates run cancellation, proposal identity, target state and expiry
before granting its exact existing permission scope. The Agent Loop then
dispatches through the registry's normal pre-handler permission check. There is
no conversion of a dispatch denial into an automatic grant. The fixture rechecks
the preview and permission immediately before its one effect and revokes the
scope after use. Approval consumes no step; successful registry dispatch consumes
exactly one. All other paths discard/revoke the proposal in cleanup.

Missing, denied, closed, expired, revoked, reused, fabricated and cross-run
decisions fail closed. Cancellation/closing denies and wakes the wait immediately.
Approval details never go to a provider, transcript, task history or logs. There
is no model-callable approve tool, wildcard, approval persistence, natural-language
approval authority or production synthetic-approval switch.

The GUI deliberately does not register WorkspaceFiles, DesktopAutomation or
BrowserAutomation. Their proposal, exact scope, target-state and expiration
contracts were reviewed; a memory fixture scope cannot authorize them. General
arbitrary-plan pause/resume and real-adapter review are unsupported. A future
adapter integration must explicitly obtain every existing required scope before
protected registry dispatch, and revalidate its own exact immutable proposal.

## Stop and shutdown

Cancel and Emergency Stop directly latch the current run's actual Cancellation
from the GUI thread. They deny/discard pending review, notify the approval waiter,
and immediately show **Stopping**. Further submissions and late progress are
ignored until the worker returns. Cancelled appears only after cancellation is
observed. If Stop races with an already completed backend result, that actual
result is preserved and the UI says it finished after Stop; it does not announce
an ordinary success or invent a backend cancellation. Earlier effects may already
have committed.

Queued signals are checked against current task identity. No subsequent run can
start before both the outcome and thread-finished notification arrive. A new run
has fresh cancellation, budgets and permissions. Workers and QThreads are deleted
through Qt lifecycle hooks after finishing. Because Qt's finished signal precedes
native thread-local cleanup, a zero-time `wait(0)` readiness check is retried by
timer until cleanup ends, before rendering the result or releasing ownership.
QThread.terminate and blocking GUI-thread waits are never used.

Closing a busy window requests cancellation and denies approval, then defers
closure while servicing events until the worker exits. Cooperative cancellation
cannot interrupt every blocked native operation or undo effects. A future blocking
integration could delay shutdown indefinitely; there is no unsafe force-kill
fallback. OS process termination cannot guarantee cleanup. This Qt consent dialog
is not an OS secure desktop and cannot defend against a malicious same-user
process injecting input or arbitrary trusted Python code modifying controllers.

## Settings and privacy

Settings supports Dark, Light and System theme, 10–18 point text and message
timestamps. Save is an explicit controller operation guarded by SETTINGS_CHANGE
for the exact validated preference values, separate from active task permissions.
Preferences are session-only; there is no QSettings or configuration-file write.
System theme follows Qt's color scheme when applied. Reopen Settings to reapply
System after a system appearance change. The UI language is English with bilingual
welcome/replies and Unicode support, not a full translation system.

Transcripts, approvals and task outcomes are ephemeral. There is no database,
cloud upload, telemetry, credential storage, automatic network call, raw tool
dump or user-file operation. The audit log contains only existing sanitized
execution metadata and fixed UI diagnostic codes. Input and review payloads
are never interpolated into diagnostics. Non-sensitive preference changes cannot
select providers, change budgets or modify any task's security grants.

Voice and microphone integration, persistent memory, scheduling and background
agents remain outside this issue. Future voice input must submit through the same
bounded controller and cannot act as approval authority for protected proposals.

## Automated verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui tests/test_ui_startup.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/sofia
.\.venv\Scripts\python.exe -m pip check
```

UI tests set `QT_QPA_PLATFORM=offscreen` only in their test session. CI installs
the ui extra and sets offscreen only on the test step for Windows/Python 3.11 and
Ubuntu/Python 3.13. Existing tests are preserved. QtTest supplies keyboard/mouse
events; tests never interact with the user's actual desktop. All test-process
network access remains forbidden by the existing fixtures, and window tests also
block unrelated process and URL launches.

Tests cover inert Unicode input, bounds, optional import behavior, duplicate
starts, actual Agent Loop results/accounting, responsive timers and settings,
approval identity/expiry/revocation/reuse, Stop races, close during work, fresh
runs, stale signals, cleanup and safe diagnostics. Test-controller decisions and
Qt test clicks are **synthetic fixtures**, not human approval. Passing offscreen
tests does not prove Windows DPI, screen-reader behavior or every native platform.

## Opt-in Windows interactive smoke test

Run `python examples/ui_demo.py` on an unlocked interactive desktop as an ordinary
user. No Administrator, real API credentials, native automation or browser is
required. Use synthetic input only:

1. Verify the welcome, layout, focus navigation and English message/reply.
2. Send Thai, mixed English/Thai, emoji, long Thai and Shift+Enter multiline text.
3. Run safe task; inspect completed state, three verified history records and
   three used steps with zero API usage.
4. Review protected demo and choose Deny; verify permission_denied and zero steps.
5. Open a new protected demo; check its different preview and approve it. Verify
   one completed, verified memory_commit and exactly one step.
6. Open another review and dismiss it; verify denial. Open another and press
   Emergency Stop from the main window; verify eventual cancellation.
7. Run slow task; open Settings while it runs and press Emergency Stop. Confirm
   Stopping precedes cancellation and a fresh safe task works afterward.
8. Save Light, Dark and System settings, font size and timestamps. Resize to the
   minimum size, maximize/minimize and test available DPI scaling. Confirm review
   buttons and Emergency Stop remain accessible, including at the largest font.
9. Close while idle, while working and while review is pending. Check for freezes,
   overlapping dialogs, QThread warnings and unexpected application launches.

Record the host, Python/Qt versions, exact steps exercised and any blockers.
Only an actual interactive run can establish the corresponding Windows smoke
results; CI and offscreen tests are not a substitute. Automated interaction with
the harmless fixture is also distinct from a person manually approving an action.

### Implementation validation record

On Windows / Python 3.14.3 / PySide6 6.10.3, the full offline suite passed with
**1,154 passed, 4 skipped**; the focused UI/startup suite passed **61 tests**.
Expected skips cover opt-in native browser/desktop integration and POSIX/Linux-only
filesystem fixtures. Ruff lint/format, mypy (49 modules), existing CLI commands and
pip dependency consistency passed. Windows sandbox filesystem virtualization
initially caused 98 existing filesystem-test failures; the unchanged suite passed
when run outside virtualization. No checks were removed or weakened.

A native Windows window was exercised through computer-use automation: mixed
English/Thai/emoji input and mock reply, safe-task progress and verified history,
actual zero API usage, Light theme, slow-work Emergency Stop, visible exact review,
Stop during review (zero dispatches), and clean closure. Diagnostics contained no
worker exceptions or QThread destruction warnings. These are automated native
smoke checks, not a completed human manual test. A person has not manually clicked
Approve/Deny; those decision paths passed synthetic offscreen Qt tests. Manual
DPI/scaling, resizing, minimize/maximize and screen-reader checks remain unverified.
No real-adapter or paid-provider integration is claimed.
