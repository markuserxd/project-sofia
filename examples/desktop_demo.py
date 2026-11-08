"""Opt-in real Windows demo; touches only the child fixture it launches."""

import argparse
from decimal import Decimal
from time import monotonic, sleep

from sofia.core import (
    AgentLoop,
    AgentPlan,
    AgentTask,
    PlannedStep,
    TaskStatus,
    VerificationResult,
)
from sofia.security import Action, OperationCancelled, PermissionDenied, RunGuardrails
from sofia.tools import ToolRegistry
from sofia.tools.desktop import DesktopAutomation, DesktopConfig
from sofia.tools.desktop_backend import fixture_application
from sofia.tools.windows_desktop import WindowsDesktopBackend


def run_demo(*, synthetic_approvals: bool = False, wait_for_focus: int = 0) -> None:
    run = RunGuardrails(max_steps=300, max_tokens=0, max_usd=Decimal(0))
    backend = WindowsDesktopBackend()
    app = fixture_application()
    adapter = DesktopAutomation(DesktopConfig((app,)), run=run, backend=backend)
    registry = ToolRegistry(run=run)
    adapter.register(registry)
    for action in (Action.DESKTOP_OBSERVE, Action.APP_LAUNCH, Action.UI_INTERACT):
        run.permissions.approve(action, adapter.permission_scope)
    run.permissions.approve(
        Action.DESKTOP_OBSERVE, adapter.target_scope(app.app_id, "observe")
    )

    def call(name, **args):
        result = registry.execute("desktop_" + name, args)
        if (
            not result.succeeded
            or not isinstance(result.output, dict)
            or result.output.get("status") != "ok"
        ):
            code = (
                result.output.get("code")
                if isinstance(result.output, dict)
                else result.error
            )
            raise RuntimeError("desktop_demo_failed:" + str(code))
        return result.output

    def approve(preview_id):
        preview = adapter.proposal(preview_id)
        if not synthetic_approvals:
            print(
                "Review fixture action:",
                preview.operation,
                preview.control_key or "window",
            )
            if preview.text is not None:
                print("Synthetic text:", preview.text)
            if (
                input("Approve this exact fixture action? [yes/no] ").strip().lower()
                != "yes"
            ):
                raise RuntimeError("user_declined")
        action = (
            Action.APP_LAUNCH if preview.operation == "launch" else Action.UI_INTERACT
        )
        run.permissions.approve(
            action,
            adapter.target_scope(
                preview.app_id,
                preview.operation,
                window_id=preview.window_id,
                control=preview.control_key,
            ),
        )
        run.permissions.approve(action, adapter.approval_scope(preview_id))

    try:
        launch = call("preview", app_id=app.app_id, operation="launch")["preview_id"]
        try:
            registry.execute("desktop_launch", {"preview_id": launch})
        except PermissionDenied:
            pass
        else:
            raise AssertionError("unapproved launch accepted")
        approve(launch)
        window_id = call("launch", preview_id=launch)["window_id"]
        windows = call("discover", app_id=app.app_id)["windows"]
        assert any(w["window_id"] == window_id for w in windows)
        rejected = registry.execute(
            "desktop_preview",
            {
                "app_id": app.app_id,
                "operation": "focus",
                "window_id": "unrelated-window",
            },
        )
        assert rejected.output == {"status": "error", "code": "target_not_found"}
        if wait_for_focus:
            print(
                "Bring the synthetic fixture window to the foreground now.", flush=True
            )
            focus_deadline = monotonic() + wait_for_focus
            for _ in range(wait_for_focus * 4):
                if monotonic() >= focus_deadline:
                    raise RuntimeError("fixture_focus_unavailable")
                if call("inspect", app_id=app.app_id, window_id=window_id)["active"]:
                    break
                sleep(0.25)
            else:
                raise RuntimeError("fixture_focus_unavailable")
        for operation, extra in (
            ("focus", {}),
            ("text", {"control": "entry", "text": "SOFIA synthetic value"}),
            ("click", {"control": "button"}),
        ):
            pid = call(
                "preview",
                app_id=app.app_id,
                operation=operation,
                window_id=window_id,
                **extra,
            )["preview_id"]
            approve(pid)
            # The real AgentLoop verifies structured outcomes separately from dispatch.
            result = AgentLoop(
                registry=registry,
                planner=lambda instruction: AgentPlan(
                    (PlannedStep("desktop_interact", {"preview_id": pid}),)
                ),
                verifier=lambda step, observation: VerificationResult(
                    isinstance(observation.output, dict)
                    and observation.output.get("status") == "ok"
                    and observation.output.get("verified") is True
                ),
                completion_verifier=lambda records: VerificationResult(
                    len(records) == 1
                ),
            ).execute(
                AgentTask("Perform the exact approved synthetic fixture action.", run)
            )
            if result.status is not TaskStatus.COMPLETED:
                raise RuntimeError(
                    "fixture_"
                    + operation
                    + "_not_verified:"
                    + (result.failure.value if result.failure else "unknown")
                )
        pending = call(
            "preview",
            app_id=app.app_id,
            operation="click",
            window_id=window_id,
            control="button",
        )["preview_id"]
        approve(pending)
        run.cancellation.cancel()
        try:
            registry.execute("desktop_interact", {"preview_id": pending})
        except OperationCancelled:
            pass
        else:
            raise AssertionError("cancelled action accepted")
        print(
            "Fixture launch, discovery, identity, focus, text, click, verification, denial and cancellation passed."
        )
        print(
            "Approval mode:",
            "synthetic test controller" if synthetic_approvals else "interactive user",
        )
    finally:
        backend.close_owned()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wait-for-focus",
        type=int,
        choices=range(61),
        default=0,
        metavar="SECONDS",
        help="observe for up to 60 seconds while an operator focuses the fixture",
    )
    parser.add_argument(
        "--run-gui",
        action="store_true",
        help="explicitly launch and interact with the fixture",
    )
    parser.add_argument(
        "--synthetic-approvals",
        action="store_true",
        help="controlled smoke test only; no real user decision",
    )
    args = parser.parse_args()
    if not args.run_gui:
        parser.error("--run-gui is required; no desktop actions have run")
    run_demo(
        synthetic_approvals=args.synthetic_approvals, wait_for_focus=args.wait_for_focus
    )


if __name__ == "__main__":
    main()
