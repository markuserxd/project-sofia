"""Deterministic, synthetic backend: never imports Windows packages or touches UI."""

from dataclasses import replace
from decimal import Decimal

from sofia.security import Action, RunGuardrails
from sofia.tools import ToolRegistry
from sofia.tools.desktop import DesktopAutomation, DesktopConfig
from sofia.tools.desktop_backend import (
    ControlInfo,
    ControlRule,
    DesktopApplication,
    WindowInfo,
)


class FakeDesktop:
    def __init__(self, app):
        self.app = app
        self.rows = [
            WindowInfo(
                10, 20, "process-generation-1", app.version.path, app.window_class
            )
        ]
        self.fields = {
            "entry": [ControlInfo((1, 10, 1001), "1001", "Edit", editable=True)],
            "button": [ControlInfo((1, 10, 1002), "1002", "Button")],
        }
        self.actions = []
        self.launches = 0
        self.focus = True
        self.verification = True
        self.running = True
        self.ready = True
        self.hook = lambda stage: None
        self.calls = []
        self.text = None

    def check(self, stage, context):
        self.calls.append(stage)
        context.checkpoint()
        self.hook(stage)
        context.checkpoint()

    def session(self, context):
        self.check("session", context)

    def windows(self, app, limit, context):
        self.check("windows", context)
        return tuple(w for w in self.rows if w.executable == app.version.path)[:limit]

    def window(self, handle, context):
        self.check("window", context)
        from sofia.tools.desktop_backend import DesktopError

        matches = [w for w in self.rows if w.handle == handle]
        if not matches:
            raise DesktopError("target_not_found")
        return matches[0]

    def controls(self, window, rule, context):
        self.check("controls", context)
        return tuple(self.fields[rule.key])

    def launch(self, app, context):
        self.check("launch", context)
        self.launches += 1
        if self.ready:
            self.rows = [
                WindowInfo(
                    10, 20, "process-generation-1", app.version.path, app.window_class
                )
            ]
        return 20, "process-generation-1"

    def alive(self, pid, created, context):
        self.check("alive", context)
        return self.running

    def focused(self, window, context):
        self.check("focused", context)
        return self.focus

    def act(self, operation, window, rule, control, text, context):
        self.check("act", context)
        self.actions.append(operation)
        if operation == "text":
            self.text = text
        if operation == "focus":
            self.focus = True

    def verify(self, operation, window, rule, text, context):
        self.check("verify", context)
        return self.verification and (operation != "text" or self.text == text)


def setup(tmp_path, *, max_steps=100, **config):
    executable = tmp_path / "benign.exe"
    executable.write_bytes(b"synthetic exe identity; never executed")
    app = DesktopApplication(
        "fixture",
        executable.resolve(),
        "SyntheticFixture",
        (
            ControlRule("entry", "1001", "Edit", "text"),
            ControlRule("button", "1002", "Button", "click", "1003", "count: 1"),
        ),
    )
    run = RunGuardrails(max_steps=max_steps, max_tokens=100, max_usd=Decimal(1))
    backend = FakeDesktop(app)
    clock = [0.0]
    registry = ToolRegistry(run=run, clock=lambda: clock[0])
    adapter = DesktopAutomation(
        DesktopConfig((app,), **config),
        run=run,
        backend=backend,
        clock=lambda: clock[0],
        pause=lambda seconds: None,
    )
    adapter.register(registry)
    for action in (Action.DESKTOP_OBSERVE, Action.APP_LAUNCH, Action.UI_INTERACT):
        run.permissions.approve(action, adapter.permission_scope)
    run.permissions.approve(
        Action.DESKTOP_OBSERVE, adapter.target_scope(app.app_id, "observe")
    )
    window_id = call(registry, "discover", app_id=app.app_id)["windows"][0]["window_id"]
    return adapter, registry, run, backend, app, window_id, clock


def call(registry, tool_name, **args):
    result = registry.execute("desktop_" + tool_name, args)
    assert result.succeeded
    return result.output


def preview(registry, window_id, *, operation="click", **extra):
    args = {"app_id": "fixture", "operation": operation}
    if operation != "launch":
        args["window_id"] = window_id
    if operation in ("text", "click"):
        args["control"] = "entry" if operation == "text" else "button"
    return call(registry, "preview", **(args | extra))["preview_id"]


def approve(adapter, run, pid):
    p = adapter.proposal(pid)
    action = Action.APP_LAUNCH if p.operation == "launch" else Action.UI_INTERACT
    run.permissions.approve(
        action,
        adapter.target_scope(
            p.app_id, p.operation, window_id=p.window_id, control=p.control_key
        ),
    )
    run.permissions.approve(action, adapter.approval_scope(pid))


def change_window(backend, **changes):
    backend.rows[0] = replace(backend.rows[0], **changes)
