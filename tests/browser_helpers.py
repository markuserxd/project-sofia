from decimal import Decimal

from sofia.security import Action, RunGuardrails
from sofia.tools import ToolRegistry
from sofia.tools.browser import BrowserAutomation
from sofia.tools.browser_policy import BrowserConfig


class FakeBrowser:
    def __init__(self):
        self.sessions = set()
        self.actions = []
        self.text = "ready"
        self.state = "exact synthetic form state"
        self.fail = False
        self.closed = False

    def start(self, session, context):
        context.checkpoint()
        self.sessions.add(session)

    def close(self, session):
        self.sessions.discard(session)

    def close_all(self):
        self.sessions.clear()
        self.closed = True

    def navigate(self, session, policy, path, context):
        self.actions.append("navigate")

    def observe(self, session, rule, context):
        return self.text

    def fill(self, session, rule, text, context):
        self.actions.append("fill")
        self.state = text
        return True

    def click(self, session, rule, context):
        self.actions.append("click")
        self.text = "changed"

    def form_state(self, session, policy, form, context):
        return self.state

    def submit(self, session, policy, form, state, context):
        self.actions.append("submit")
        if self.fail:
            raise RuntimeError("DO NOT LOG: private form content")

    def download_state(self, session, policy, rule, context):
        return "exact synthetic link"

    def download(self, session, policy, rule, context):
        self.actions.append("download")
        return rule.body


def call(registry, operation, **args):
    result = registry.execute("browser_" + operation, args)
    assert result.succeeded, result.error
    return result.output


def grant(adapter, run, operation, *, session="", target=""):
    action = adapter._action(operation)
    run.permissions.approve(action, adapter.permission_scope)
    run.permissions.approve(
        action,
        adapter.target_scope("fixture", operation, session_id=session, target=target),
    )


def approve(adapter, run, preview_id):
    p = adapter.proposal(preview_id)
    grant(adapter, run, p.operation, session=p.session_id, target=p.target)
    run.permissions.approve(
        Action.FORM_SUBMIT if p.operation == "submit" else Action.FILE_WRITE,
        adapter.approval_scope(preview_id),
    )


def setup(policy, *, backend=None, quarantine=None, clock=None, **limits):
    run = RunGuardrails(max_steps=200, max_tokens=0, max_usd=Decimal(0))
    registry = ToolRegistry(run=run)
    backend = backend if backend is not None else FakeBrowser()
    extra = {} if clock is None else {"clock": clock}
    adapter = BrowserAutomation(
        BrowserConfig((policy,), **limits),
        run=run,
        backend=backend,
        quarantine=quarantine,
        exclusive_access=quarantine is not None,
        **extra,
    )
    adapter.register(registry)
    grant(adapter, run, "start")
    sid = call(registry, "start", origin_id="fixture")["session_id"]
    grant(adapter, run, "navigate", session=sid, target="/")
    assert (
        call(registry, "navigate", session_id=sid, url=policy.origin + "/")["status"]
        == "ok"
    )
    return adapter, registry, run, backend, sid
