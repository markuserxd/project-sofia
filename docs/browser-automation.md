# Controlled Playwright browser automation

Issue [SOFIA-012](https://github.com/markuserxd/project-sofia/issues/12) adds an
explicitly constructed, synchronous Chromium adapter for **synthetic loopback
fixtures only**. Remote websites, authenticated browsing and arbitrary website
scripts are unsupported and fail closed. The ordinary CLI does not register
browser tools, import Playwright, launch Chromium, install binaries or contact a
website. Browser support is independent of the Windows desktop extra.

## Installation and supported environments

```powershell
python -m pip install -e ".[dev,browser,openai,anthropic]"
python -m playwright install chromium
```

The optional extra is `playwright>=1.64.1,<1.65`. Version 1.64.1 was verified
against [PyPI](https://pypi.org/project/playwright/1.64.1/) and installed locally; its installer selected Chromium 156.0.8078.4
(revision 1248). Reinstall matching binaries when upgrading Playwright.
Missing dependencies and launch/binary failures produce fixed sanitized codes.
Nothing installs automatically at application startup.

Native verification was performed on Windows / Python 3.14.3. CI retains Windows
/ Python 3.11 and Ubuntu / Python 3.13 for offline tests, and explicitly installs
and runs real Chromium on Windows. Linux native execution requires Playwright's
system libraries and a non-root environment that supports Chromium's sandbox.
Ubuntu CI intentionally does not claim native browser coverage. No firewall,
administrator configuration or sandbox bypass is part of the application.

## Architecture and public interfaces

`sofia.tools.browser_policy` holds immutable `BrowserConfig`,
`BrowserOriginPolicy`, `FixtureDocument`, `BrowserElement`, `BrowserForm` and
`BrowserDownload` contracts. They are created by trusted controller code, never
from model output or fetched page content. A manifest contains exact expected
fixture response bytes; the controller must own and review the fixture server.
Do not turn a remote response into a manifest at runtime.

`sofia.tools.browser_backend` defines the typed `BrowserBackend` protocol,
private native `PlaywrightBrowserBackend` and bounded HTTP broker.
`sofia.tools.browser.BrowserAutomation` owns sessions, permissions, previews,
quarantine publication and audit. No native page/context/browser is returned.
The backend is trusted Python, not an in-process sandbox.

```python
from sofia.tools import BrowserAutomation, BrowserConfig, ToolRegistry

# policy is a controller-owned BrowserOriginPolicy for its running fixture.
# run is the existing RunGuardrails, shared with the registry and AgentTask.
registry = ToolRegistry(run=run)
with BrowserAutomation(BrowserConfig((policy,)), run=run) as browser:
    browser.register(registry)
    # Obtain actual user decisions in controller code before granting scopes.
    # Execute exclusively through registry.execute(...).
```

The complete executable example is `examples/browser_demo.py`; its explicit
`--synthetic-approvals` flag authorizes only its disposable synthetic scenario.
It is not an approval UI or an example of approving real submissions automatically.

Registered tools:

| Tool | Arguments | Existing Action |
| --- | --- | --- |
| `browser_start` | `origin_id` | `APP_LAUNCH` |
| `browser_navigate` | `session_id`, `url` | `UI_INTERACT` |
| `browser_observe` | `session_id`, `target` | `SENSITIVE_READ` |
| `browser_fill` | `session_id`, `target`, `text` | `UI_INTERACT` |
| `browser_click` | `session_id`, `target` | `UI_INTERACT` |
| `browser_preview_submit` | `session_id`, `target` | `SENSITIVE_READ` |
| `browser_submit` | `preview_id` | `FORM_SUBMIT` |
| `browser_preview_download` | `session_id`, `target` | `SENSITIVE_READ` |
| `browser_download` | `preview_id` | `FILE_WRITE` |
| `browser_close` | `session_id` | `UI_INTERACT` |

`register(registry, prefix=...)` requires identity with the shared run and rejects
duplicate registration. `permission_scope`, `target_scope(origin_id, operation,
session_id=..., target=...)`, `proposal(preview_id)`, `approval_scope(preview_id)`
and `discard(preview_id)` are trusted controller helpers, never tools. A local
proposal includes private exact review data and must not be sent to a provider.
`close()` and the context manager provide unconditional trusted cleanup even
after cancellation or budget exhaustion. Always use `finally` or `with`.

## Origins, network enforcement and residual risks

The default origin tuple is empty. The only accepted spelling is
`http://127.0.0.1:<port>` with a port from 1024 through 65535. Each origin is exact;
scheme, hostname and port must match. There are no subdomain, prefix, wildcard,
DNS, IDNA, IPv6, unusual numeric IP, credentials, proxy or HTTPS downgrade modes.
Queries, fragments, escaped/ambiguous paths, control characters and non-ASCII
URLs are rejected. Only manifest navigation paths can be selected. Loopback is
an explicit fixture exception, never a general grant to internal services.

Chromium contexts stay **offline**. Every ordinary browser request is aborted
unless there is one pending controller operation ticket for the retained main
frame, exact URL, method and (for forms) exact encoded payload. Tickets are
consumed once. There is no `route.continue_`, `route.fetch`, Chromium proxy or
browser-directed remote connection. The Python broker connects directly to the
configured numeric loopback address and port, ignores proxy environment variables,
and forwards no browser cookies, storage, authentication or request headers.
Only GET and exact reviewed POST operations are supported.

The broker rejects every non-200 response, including all redirects, without
following Location. It requires the exact expected Content-Length, rejects
compressed bodies, reads bounded chunks and verifies exact manifest bytes before
fulfilling a response. Server headers are discarded. This avoids relying on
Playwright routing to intercept subsequent redirect hops. Detecting a final URL
is only a postcondition, never the redirect prevention mechanism.

Every document receives a restrictive adapter-owned CSP: default sources, frames,
workers, objects, external scripts, connections and base tags are denied; forms
are restricted to the same origin and still require an operation ticket. Only
the SHA-256 hash of the fixed, reviewed `FIXTURE_SCRIPT` is executable. It sets
one synthetic status label after one benign button click. A manifest cannot add
executable scripts, inline event handlers, eval, WebRTC or network-capable page
code. Backend locator inspections use fixed trusted code with separate data
arguments; there is no model-accessible evaluate/JavaScript tool.

WebSockets are denied by offline networking and CSP. Service workers are blocked;
frames and their interaction are denied. Unexpected pages/popups are closed and
never become session targets; their initial requests have no authorization.
Dialogs are dismissed, file choosers receive no files, and no browser permissions
are granted. Callback failures are latched and sanitized instead of entering
asyncio's raw exception logger.

**Limits of this threat model:** offline emulation does not constrain WebRTC UDP.
See the [official offline API documentation](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-set-offline).
The supported mode therefore forbids arbitrary renderer scripts and exposes no
WebRTC operation; enabling general website JS would invalidate this design.
Fresh contexts and CSP are not OS process/network isolation or proof of complete
SSRF prevention. Browser vulnerabilities, browser-process background behavior,
hostile local processes, compromised trusted Python and hostile trusted fixtures
are outside this boundary. No production remote browsing mode exists. Do not
relax origin, script or request rules to automate an arbitrary site.

## Lifecycle, observation and interaction

Each session has an opaque ownership ID, a separate opaque page ID, exactly one
retained page, a fresh nonpersistent context and empty cookie/storage state.
No profile discovery, CDP, credential imports, extensions, saved storage,
permissions, tracing, HAR, video or screenshots are enabled. Chromium is launched
with its sandbox explicitly enabled and no custom switches. Contexts isolate
cookies, localStorage, sessionStorage and IndexedDB between sessions.

Targets are exact trusted `data-testid` values selected by configured keys.
Missing/duplicate, hidden, disabled, contenteditable and frame targets fail
closed. Observation is restricted to known span/paragraph/output labels and
transfers at most 256 characters, marked as untrusted data. It never reads inputs,
passwords, hidden fields, full HTML, cookies or arbitrary page text. There is no
implicit release to an LLM provider.

Fill supports visible enabled writable text inputs only, with printable bounded
synthetic content. Password, file, payment/autofill-sensitive and unsupported
controls are rejected. Click supports only controller-assessed standalone
`type=button` controls with a configured status-label postcondition. DOM type
alone does not make an action benign. Other consequential clicks are unsupported.

## Sensitive submissions and previews

Preview inspects one configured native POST form without sending it. It rejects
unknown/hidden/disabled/non-text fields, duplicate or unexpected field names,
extra control types, alternative destinations/methods and submit overrides.
Review data binds the adapter, session, origin, page identity/version, form key,
destination, method, exact field values, bounded form markup, digest and expiry.
State changes through navigation/fill/click invalidate pending previews.

Execution requires coarse `FORM_SUBMIT`, the exact origin/session/form operation
scope and the exact preview scope. Only controller code can record approval in
the existing PermissionPolicy. There is no approval tool or `approved` argument.
Approval can be revoked/discarded before admission. Admission consumes and
revokes the one-shot capability even if state revalidation or the request fails.
The exact state is revalidated immediately before click and the request ticket
also checks exact POST bytes, so a late payload/destination change is rejected.

Background native form/fetch/XHR/GET submissions have no ticket. A successful
submission requires the exact expected fixture response and resulting page URL.
A timeout after dispatch can mean an effect occurred: do not retry submissions
or downloads automatically. Permission denial leaves the proposal available for
review; a failed/uncertain native action closes its session.

## Downloads and filesystem boundary

No quarantine means downloads are disabled. A controller may provide a dedicated
existing local quarantine directory and explicitly assert `exclusive_access=True`.
It must exclude other writers through publication. This is a separate root with
its own FILE_WRITE scopes; it does not inherit or bypass WorkspaceFiles approvals.
No WorkspaceFiles private methods or text API are used for binary persistence.
The adapter reuses the existing non-following directory primitives, checks/pins
root identity and rejects unsupported volumes/reparse ancestors.

Only manifest-known printable UTF-8 `.txt` bytes, at most 4096 bytes, are accepted.
Preview binds the link/page state, exact content digest and generated destination
name. It performs no download. Fresh exact FILE_WRITE approval is required for
the initiating action and persistence. Browser suggested names must match the
safe configured name; traversal, separators, reserved Windows names, ADS, multiple
extensions and executable/archive formats are rejected. The controller-generated
quarantine basename is used for publication, never a server path.

The broker bounds and verifies the complete known body **before** handing it to
Chromium's attachment download. Playwright may temporarily write those bounded
bytes; it then deletes that artifact. Unexpected downloads are cancelled, and
browser contexts clean their artifacts on close. The final quarantine write uses
a private temporary file, fsync and atomic no-clobber publication. Existing files
are never overwritten; partial files are removed where cleanup succeeds.
Downloads are never opened, executed or extracted. Quarantine files remain for
the controller to inspect/remove; the demo owns a TemporaryDirectory and removes
them itself.

These checks bound application reads and supported fixture bytes, not TCP traffic
already buffered by the OS or a malicious server's transmission. Unrestricted
streams, arbitrary attachments, antivirus and archive safety are unsupported.
Cleanup failure is reported through sanitized failure codes; a controller must
inspect possible partial/committed artifacts locally and must not blindly retry.

## Resource limits, cancellation and verification

Defaults are two sessions, one retained page per session, 64 operations per
session, 120s session admission lifetime, 5s tool/native timeout, 30s preview TTL,
16 pending previews, 256 fill characters and two downloads per session. Manifests
allow up to four origins, 16 documents/elements per origin, four forms/downloads,
eight text fields per form (512 characters each) and 64 KiB per document.
Configuration validates finite hard upper bounds. The example uses 15s finite
operation timeouts to accommodate cold browser startup.

Checkpoints run before/after native actions, request admission, broker reads and
file publication. Registry dispatch accounts exactly one shared step; handlers
only check additional permissions and never call begin_step again. Cancellation
and accounting uncertainty block new work. Expired sessions are closed at the
next operation/start check; there is no lifetime-monitor worker. Session and
preview limits govern admission, not hard process CPU/memory quotas.

Cancellation/timeouts are cooperative, not hard interruption of Playwright,
socket, filesystem or cleanup calls. Already-sent effects cannot be undone.
Always close the adapter after stop, denial or exhausted budgets. There are no
polling/retry/replanning workers. The integration harness additionally bounds its
child process with a 180s timeout.

AgentLoop needs a deterministic trusted planner, a step verifier that checks
both `ToolResult.succeeded` and `output["status"]` (plus actual `verified` or
observed postconditions), and a task-wide completion verifier. Keep submission
and download tools out of retry allowlists. Whole-plan validation is unchanged.
No provider calls or observation uploads are part of browser execution.

## Audit and tests

The existing SOFIA logging infrastructure receives fixed operation/status codes
and whether exact approval was checked. Registry events keep opaque invocation
IDs. Browser events contain no URLs, paths, DOM, payloads, cookies, form values,
digests, downloaded bytes, raw exceptions or protocol traces. Audit persistence
is best effort; logging failure cannot grant authority. Do not enable Playwright
protocol debugging/traces in a controller handling private data.

Offline tests keep the existing blanket socket prohibition:

```powershell
python -m pytest tests/test_browser_policy.py tests/test_browser_security.py tests/test_browser_backend.py tests/test_browser_downloads.py -v
python -m pytest -v
```

The real test runs a separate child process with two standard-library servers
bound only to 127.0.0.1 on ephemeral ports. All data and approval decisions are
synthetic; no public websites, real recipients, paid APIs or credentials are used.
The attack fixture also uses trusted test-only JavaScript injection to exercise
routing independently of the production script CSP. This test access is never
registered as a tool.

```powershell
python examples/browser_demo.py --synthetic-approvals
$env:SOFIA_BROWSER_INTEGRATION = '1'
python -m pytest tests/test_browser_integration.py -v
Remove-Item Env:SOFIA_BROWSER_INTEGRATION
```

If installing browsers under the repository rather than the default cache, set
`PLAYWRIGHT_BROWSERS_PATH` to an absolute controller-owned directory before both
installation and execution (local verification used `.tmp/playwright`).

Mocks verify limits, permissions, stale/revoked/foreign approvals, transport
errors, cancellation, malformed selectors, logging privacy, stream limits,
quarantine failure cleanup and AgentLoop failures. Real Chromium verifies
navigation, fill/click outcomes, cookie/localStorage/sessionStorage isolation,
exact one-shot native form approval, attachment download/publication, redirect
and escape rejection, changed-response rejection, cancellation and cleanup.
Passing tests establish behavior for this fixture and environment; they do not
establish complete SSRF protection or safe arbitrary website automation.

Local verification on 2026-11-08 (Windows / Python 3.14.3): the full suite passed
1,093 tests with four expected skips; the focused offline browser suite passed
119 tests; the opt-in real Chromium pytest passed one scenario. Ruff lint and
format checks, mypy for Windows and Linux target platforms (39 source files), CLI
help/version/default, and pip check passed. The native scenario also passed as a
standalone demo. GitHub CI results are reported separately on the implementation
PR; local validation is not a claim that Ubuntu native Chromium ran.
