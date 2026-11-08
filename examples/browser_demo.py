"""Opt-in, synthetic-only real Chromium scenario; no personal accounts or model APIs."""

import argparse
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from browser_test_server import DOWNLOAD, FixtureDocument, fixture_policy, local_fixture

from sofia.core import (
    AgentLoop,
    AgentPlan,
    AgentTask,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.security import OperationCancelled, PermissionDenied, RunGuardrails
from sofia.tools import ToolRegistry
from sofia.tools.browser import BrowserAutomation
from sofia.tools.browser_backend import PlaywrightBrowserBackend
from sofia.tools.browser_policy import BrowserConfig


def run_demo() -> None:
    with (
        local_fixture() as server,
        local_fixture() as victim,
        TemporaryDirectory(prefix="sofia-browser-") as directory,
    ):
        policy = fixture_policy(server.origin)
        run = RunGuardrails(max_steps=150, max_tokens=0, max_usd=Decimal(0))
        registry = ToolRegistry(run=run)
        backend = PlaywrightBrowserBackend(timeout=15)
        with BrowserAutomation(
            BrowserConfig((policy,), timeout=15),
            run=run,
            backend=backend,
            # Windows runners may supply a DOS short-name TEMP path. Expand the
            # controller-created root before applying the unchanged root checks.
            quarantine=Path(directory).resolve(strict=True),
            exclusive_access=True,
        ) as adapter:
            adapter.register(registry)

            def grant(operation, sid="", target=""):
                action = adapter._action(operation)
                run.permissions.approve(action, adapter.permission_scope)
                run.permissions.approve(
                    action,
                    adapter.target_scope(
                        "fixture", operation, session_id=sid, target=target
                    ),
                )

            def call(operation, **args):
                result = registry.execute("browser_" + operation, args)
                assert result.succeeded, result.error
                assert isinstance(result.output, dict), "invalid browser output"
                return result.output

            def ok(operation, **args):
                output = call(operation, **args)
                assert output["status"] == "ok", output.get("code")
                return output

            def approve(pid):
                proposal = adapter.proposal(pid)
                grant(proposal.operation, proposal.session_id, proposal.target)
                run.permissions.approve(
                    adapter._action(proposal.operation), adapter.approval_scope(pid)
                )

            grant("start")
            sid = ok("start", origin_id="fixture")["session_id"]
            grant("navigate", sid, "/")
            ok("navigate", session_id=sid, url=server.origin + "/")
            grant("observe", sid, "status")
            assert ok("observe", session_id=sid, target="status")["text"] == "ready"
            grant("fill", sid, "message")
            grant("click", sid, "benign")
            grant("preview_submit", sid, "contact")

            # Real AgentLoop uses one shared guardrail; verify actual backend results.
            evidence = []

            def verify(step, observation):
                evidence.append(observation.output)
                return VerificationResult(
                    isinstance(observation.output, dict)
                    and observation.output.get("status") == "ok"
                    and observation.output.get("verified") is True
                )

            before = run.budget.snapshot.steps
            result = AgentLoop(
                registry=registry,
                planner=lambda _: AgentPlan(
                    (
                        PlannedStep(
                            "browser_fill",
                            {
                                "session_id": sid,
                                "target": "message",
                                "text": "synthetic",
                            },
                        ),
                        PlannedStep(
                            "browser_click", {"session_id": sid, "target": "benign"}
                        ),
                    )
                ),
                verifier=verify,
                completion_verifier=lambda records: VerificationResult(
                    len(records) == 2 and len(evidence) == 2
                ),
            ).execute(AgentTask("Fill and click the known synthetic fixture", run))
            assert result.status is TaskStatus.COMPLETED, result.failure
            assert run.budget.snapshot.steps == before + 2
            assert ok("observe", session_id=sid, target="status")["text"] == "changed"

            # Fixture tests may inspect private native objects; these are never tools.
            native, page = backend._sessions[sid]
            page.evaluate(
                "localStorage.setItem('synthetic','one'); sessionStorage.setItem('synthetic','one'); document.cookie='synthetic=one'"
            )
            other = ok("start", origin_id="fixture")["session_id"]
            grant("navigate", other, "/")
            ok("navigate", session_id=other, url=server.origin + "/")
            other_native, other_page = backend._sessions[other]
            assert not other_native.cookies()
            assert other_page.evaluate(
                "[localStorage.getItem('synthetic'),sessionStorage.getItem('synthetic')]"
            ) == [None, None]
            assert native is not other_native
            grant("close", other)
            ok("close", session_id=other)

            pid = ok("preview_submit", session_id=sid, target="contact")["preview_id"]
            assert server.counts[("POST", "/submitted")] == 0
            grant("submit", sid, "contact")
            try:
                call("submit", preview_id=pid)
                raise AssertionError("unapproved submission admitted")
            except PermissionDenied:
                pass
            approve(pid)
            ok("submit", preview_id=pid)
            assert server.counts[("POST", "/submitted")] == 1
            assert server.last_submission == b"message=synthetic"
            assert ok("observe", session_id=sid, target="status")["text"] == "submitted"
            assert call("submit", preview_id=pid)["status"] == "error"

            grant("preview_download", sid, "text")
            pid = ok("preview_download", session_id=sid, target="text")["preview_id"]
            assert server.counts[("GET", "/download")] == 0
            grant("download", sid, "text")
            try:
                call("download", preview_id=pid)
                raise AssertionError("unapproved download admitted")
            except PermissionDenied:
                pass
            approve(pid)
            destination = adapter.proposal(pid).destination
            ok("download", preview_id=pid)
            assert (Path(directory) / destination).read_bytes() == DOWNLOAD
            assert len(list(Path(directory).iterdir())) == 1
            assert server.counts[("GET", "/download")] == 1

            # Redirect broker never follows even a loopback hop; victim sees nothing.
            server.redirect = victim.origin + "/denied"
            grant("navigate", sid, "/redirect")
            assert (
                call("navigate", session_id=sid, url=server.origin + "/redirect")[
                    "status"
                ]
                == "error"
            )
            assert not victim.counts

            # Explicit malicious fixture code exercises request gating, iframe/popup,
            # fetch/XHR/WebSocket, auto-form and script/meta navigation. Only the
            # reviewed attack document is fetched by the broker; no ticket remains.
            attacks = (
                (
                    """<!doctype html><html><body><p data-testid="status">attack</p>
<iframe src="VICTIM/iframe"></iframe><form method="post" action="VICTIM/post"></form>
<script>
fetch('VICTIM/fetch').catch(()=>{});
const x = new XMLHttpRequest(); x.open('GET','VICTIM/xhr'); x.send();
try { new WebSocket('WS/socket'); } catch(e) {}
window.open('VICTIM/popup');
document.querySelector('form').submit();
setTimeout(()=>{location.href='VICTIM/script';},30);
</script><meta http-equiv="refresh" content="1;url=VICTIM/meta"></body></html>"""
                )
                .replace("VICTIM", victim.origin)
                .replace("WS", victim.origin.replace("http:", "ws:"))
                .encode()
            )
            server.pages["/attack"] = attacks
            attack_policy = replace(
                policy,
                documents=policy.documents + (FixtureDocument("/attack", attacks),),
            )
            assert not backend._sessions
            # Reuse the idle synchronous driver on this thread, with fresh contexts.
            with BrowserAutomation(
                BrowserConfig((attack_policy,), timeout=15), run=run, backend=backend
            ) as attack_adapter:
                attack_registry = ToolRegistry(run=run)
                attack_adapter.register(attack_registry)

                def attack_call(operation, args, session="", target=""):
                    action = attack_adapter._action(operation)
                    run.permissions.approve(action, attack_adapter.permission_scope)
                    run.permissions.approve(
                        action,
                        attack_adapter.target_scope(
                            "fixture", operation, session_id=session, target=target
                        ),
                    )
                    return attack_registry.execute("browser_" + operation, args)

                aid = attack_call("start", {"origin_id": "fixture"}).output[
                    "session_id"
                ]
                attack_call(
                    "navigate",
                    {"session_id": aid, "url": server.origin + "/attack"},
                    aid,
                    "/attack",
                )
                attack_backend = attack_adapter._backend
                if aid in attack_backend._sessions:
                    attack_page = attack_backend._sessions[aid][1]
                    # Test-only trusted injection bypasses script-src to exercise
                    # routing too. Production tools expose no such operation.
                    attack_script = (
                        attacks.decode().split("<script>")[1].split("</script>")[0]
                    )
                    attack_page.evaluate("() => {" + attack_script + "}")
                    attack_page.wait_for_timeout(1200)
                    assert len(attack_backend._sessions[aid][0].pages) == 1
                assert not victim.counts
                assert server.counts[("GET", "/attack")] == 1

            # A changed/unreviewed server body never executes in Chromium.
            fresh = ok("start", origin_id="fixture")["session_id"]
            server.pages["/"] = b"<script>throw Error('unreviewed')</script>"
            grant("navigate", fresh, "/")
            assert (
                call("navigate", session_id=fresh, url=server.origin + "/")["code"]
                == "fixture_response_denied"
            )
            server.pages["/"] = policy.documents[0].body

            stopped = ok("start", origin_id="fixture")["session_id"]
            grant("navigate", stopped, "/")
            run.cancellation.cancel()
            before = sum(server.counts.values())
            try:
                call("navigate", session_id=stopped, url=server.origin + "/")
                raise AssertionError("cancelled operation admitted")
            except OperationCancelled:
                pass
            assert sum(server.counts.values()) == before
        assert not backend._sessions and backend._driver is None
    print(
        "Real Chromium: navigation, interaction, isolation, approvals, download, redirect/escape denial, cancellation and cleanup passed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic-approvals",
        action="store_true",
        help="Explicitly authorize the isolated synthetic fixture scenario",
    )
    args = parser.parse_args()
    if not args.synthetic_approvals:
        parser.error("This fixture requires explicit --synthetic-approvals")
    run_demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
