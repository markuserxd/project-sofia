import logging
import sys
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest
from desktop_helpers import approve, call, change_window, preview, setup

from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
    StepLimitExceeded,
)
from sofia.tools import ToolRegistry
from sofia.tools.desktop import DesktopConfig
from sofia.tools.desktop_backend import ControlRule, DesktopApplication, DesktopError
from sofia.tools.windows_desktop import WindowsDesktopBackend


def test_empty_allowlist_and_registration_identity(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    assert DesktopConfig().applications == ()
    assert call(registry, "discover", app_id="unknown") == {
        "status": "error",
        "code": "unknown_application",
    }
    other_run = RunGuardrails(max_steps=1, max_tokens=0, max_usd=Decimal(0))
    with pytest.raises(DesktopError, match="invalid_registration"):
        adapter.register(ToolRegistry(run=other_run))
    with pytest.raises(DesktopError, match="invalid_registration"):
        adapter.register(registry)
    with pytest.raises(DesktopError, match="invalid_config"):
        DesktopConfig((app, app))


@pytest.mark.parametrize(
    "changes",
    [
        {"executable": Path("relative.exe")},
        {"executable": Path("missing.exe").absolute()},
        {"arguments": ["arg"]},
        {"arguments": ("\n",)},
        {"multiple_instances": 1},
        {"app_id": "*"},
        {"window_class": ""},
    ],
)
def test_invalid_configuration(tmp_path, changes):
    *_, app, wid, clock = setup(tmp_path)
    with pytest.raises(DesktopError):
        replace(app, **changes)


@pytest.mark.parametrize(
    "name",
    [
        "cmd",
        "powershell",
        "python",
        "python314",
        "msiexec",
        "msedge",
        "regedit",
        "keepass",
    ],
)
def test_sensitive_executables_blocked(tmp_path, name):
    path = tmp_path / (name + ".exe")
    path.write_bytes(b"synthetic")
    with pytest.raises(DesktopError, match="unsafe_target"):
        DesktopApplication("unsafe", path.resolve(), "window")


@pytest.mark.parametrize(
    "args",
    [
        {"executable": "cmd.exe"},
        {"arguments": ["/c", "anything"]},
        {"approved": True},
        {"x": 10, "y": 20},
        {"hotkey": "ctrl+l"},
    ],
)
def test_model_cannot_supply_execution_authority(tmp_path, args):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    before = run.budget.snapshot.steps
    result = registry.execute(
        "desktop_preview", {"app_id": "fixture", "operation": "launch", **args}
    )
    assert result.error == "invalid_arguments"
    assert run.budget.snapshot.steps == before
    assert not backend.actions and backend.launches == 0
    assert not any("approve" in tool.name for tool in registry.list_tools())


def test_non_windows_fails_closed_without_imports(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(DesktopError, match="unsupported_platform"):
        WindowsDesktopBackend()


def test_discovery_privacy_bounds_and_no_activation(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path, max_windows=2)
    backend.rows.append(
        replace(backend.rows[0], handle=11, pid=99, executable=Path("unrelated.exe"))
    )
    result = call(registry, "discover", app_id="fixture")
    assert len(result["windows"]) == 1
    assert set(result["windows"][0]) == {"window_id", "visible", "enabled", "minimized"}
    assert not backend.actions
    inspected = call(registry, "inspect", app_id="fixture", window_id=wid)
    assert all(
        set(c) == {"control", "type", "enabled", "visible"}
        for c in inspected["controls"]
    )
    backend.rows.extend(
        [replace(backend.rows[0], handle=11), replace(backend.rows[0], handle=12)]
    )
    assert call(registry, "discover", app_id="fixture")["code"] == "target_limit"


def test_identical_titles_do_not_conflate_windows_and_duplicates_rejected(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    backend.rows.append(replace(backend.rows[0], handle=11))
    rows = call(registry, "discover", app_id="fixture")["windows"]
    assert len({row["window_id"] for row in rows}) == 2
    backend.rows.append(backend.rows[0])
    assert call(registry, "discover", app_id="fixture")["code"] == "ambiguous_target"


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": 21},
        {"runtime_id": (42, 999)},
        {"created": "reused-process"},
        {"executable": Path("other.exe")},
        {"window_class": "other"},
        {"visible": False},
        {"enabled": False},
        {"minimized": True},
    ],
)
def test_changed_target_burns_approval(tmp_path, changes):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    change_window(backend, **changes)
    assert call(registry, "interact", preview_id=pid)["status"] == "error"
    assert not backend.actions
    assert call(registry, "interact", preview_id=pid)["code"] == "preview_not_found"


def test_closed_window_rejected(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    backend.rows.clear()
    assert call(registry, "interact", preview_id=pid)["code"] == "target_not_found"
    assert not backend.actions


def test_exact_approval_one_shot_and_step_accounting(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    assert not backend.actions
    with pytest.raises(PermissionDenied):
        call(registry, "interact", preview_id=pid)
    approve(adapter, run, pid)
    scope = adapter.approval_scope(pid)
    before = run.budget.snapshot.steps
    assert call(registry, "interact", preview_id=pid)["verified"]
    assert run.budget.snapshot.steps == before + 1
    assert backend.actions == ["click"]
    with pytest.raises(PermissionDenied):
        run.permissions.check(Action.UI_INTERACT, scope)
    assert call(registry, "interact", preview_id=pid)["code"] == "preview_not_found"
    assert backend.actions == ["click"]


@pytest.mark.parametrize(
    "wrong", ["application", "control", "operation", "preview", "action", "revoked"]
)
def test_scopes_cannot_be_substituted(tmp_path, wrong):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    if wrong == "application":
        run.permissions.revoke(Action.UI_INTERACT, adapter.permission_scope)
    elif wrong in ("control", "operation"):
        scope = adapter.target_scope(
            "fixture", "click", window_id=wid, control="button"
        )
        run.permissions.revoke(Action.UI_INTERACT, scope)
        run.permissions.approve(
            Action.UI_INTERACT,
            adapter.target_scope(
                "fixture",
                "text" if wrong == "operation" else "click",
                window_id=wid,
                control="entry",
            ),
        )
    else:
        run.permissions.revoke(Action.UI_INTERACT, adapter.approval_scope(pid))
        run.permissions.approve(
            Action.APP_LAUNCH if wrong == "action" else Action.UI_INTERACT,
            adapter.approval_scope(pid) if wrong != "preview" else "forged-preview",
        )
        if wrong == "revoked":
            run.permissions.revoke(Action.UI_INTERACT, adapter.approval_scope(pid))
    with pytest.raises(PermissionDenied):
        call(registry, "interact", preview_id=pid)
    assert not backend.actions


def test_preview_payload_and_operation_immutable_and_expiring(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid, operation="text", text="synthetic one")
    other = preview(registry, wid, operation="text", text="synthetic two")
    approve(adapter, run, pid)
    with pytest.raises(FrozenInstanceError):
        adapter.proposal(pid).text = "changed"
    assert adapter.proposal(pid).text_digest != adapter.proposal(other).text_digest
    with pytest.raises(PermissionDenied):
        call(registry, "interact", preview_id=other)
    assert (
        registry.execute(
            "desktop_interact", {"preview_id": pid, "text": "changed"}
        ).error
        == "invalid_arguments"
    )
    assert call(registry, "launch", preview_id=pid)["code"] == "preview_mismatch"
    clock[0] = 61
    assert call(registry, "interact", preview_id=pid)["code"] == "stale_preview"
    assert (
        call(registry, "interact", preview_id="forged")["code"] == "preview_not_found"
    )
    assert not backend.actions


@pytest.mark.parametrize(
    "changes",
    [
        {"password": True},
        {"editable": False},
        {"visible": False},
        {"enabled": False},
        {"runtime_id": (9,)},
        {"control_type": "Document"},
        {"automation_id": "other"},
    ],
)
def test_control_revalidation_and_sensitive_fields(tmp_path, changes):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid, operation="text", text="safe synthetic text")
    approve(adapter, run, pid)
    backend.fields["entry"] = [replace(backend.fields["entry"][0], **changes)]
    assert call(registry, "interact", preview_id=pid)["status"] == "error"
    assert not backend.actions


@pytest.mark.parametrize(
    "count,code", [(0, "target_not_found"), (2, "ambiguous_target")]
)
def test_missing_and_ambiguous_controls(tmp_path, count, code):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    backend.fields["button"] *= count
    assert (
        call(
            registry,
            "preview",
            app_id="fixture",
            operation="click",
            window_id=wid,
            control="button",
        )["code"]
        == code
    )
    assert not backend.actions


@pytest.mark.parametrize(
    "text", ["", "x" * 257, "\n", "\t", "\x00", "\x7f", "a\u202eb"]
)
def test_text_bounds_and_controls(tmp_path, text):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    assert (
        call(
            registry,
            "preview",
            app_id="fixture",
            operation="text",
            window_id=wid,
            control="entry",
            text=text,
        )["code"]
        == "unsafe_text"
    )


def test_semantic_text_and_focus_verification(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    backend.focus = False
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    assert call(registry, "interact", preview_id=pid)["code"] == "focus_mismatch"
    focus = preview(registry, wid, operation="focus")
    approve(adapter, run, focus)
    assert call(registry, "interact", preview_id=focus)["verified"]
    pid = preview(registry, wid, operation="text", text="literal {ENTER} ^%+ synthetic")
    approve(adapter, run, pid)
    assert call(registry, "interact", preview_id=pid)["verified"]
    assert backend.text == "literal {ENTER} ^%+ synthetic"
    assert backend.actions == ["focus", "text"]


@pytest.mark.parametrize(
    "stage", ["session", "window", "controls", "focused", "act", "verify"]
)
def test_cancellation_at_interaction_boundaries(tmp_path, stage):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    backend.hook = lambda current: (
        run.cancellation.cancel() if current == stage else None
    )
    with pytest.raises(OperationCancelled):
        call(registry, "interact", preview_id=pid)
    assert len(backend.actions) == (1 if stage == "verify" else 0)
    with pytest.raises(OperationCancelled):
        call(registry, "discover", app_id="fixture")


def test_stop_before_approval_discovery_and_between_actions(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    first = preview(registry, wid)
    second = preview(registry, wid)
    approve(adapter, run, first)
    approve(adapter, run, second)
    call(registry, "interact", preview_id=first)
    run.cancellation.cancel()
    for operation, args in (
        ("discover", {"app_id": "fixture"}),
        ("interact", {"preview_id": second}),
    ):
        with pytest.raises(OperationCancelled):
            call(registry, operation, **args)
    with pytest.raises(OperationCancelled):
        adapter.approval_scope(second)
    assert backend.actions == ["click"]


@pytest.mark.parametrize("blocked", [False, True])
def test_run_limits_prevent_input(tmp_path, blocked):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path, max_steps=3)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    if blocked:
        reservation = run.budget.reserve(estimated_tokens=1, estimated_usd=Decimal(0))
        with pytest.raises(AccountingBlocked):
            run.budget.settle(reservation, actual_tokens=None, actual_usd=None)
    else:
        run.budget.consume_step()
    with pytest.raises(AccountingBlocked if blocked else StepLimitExceeded):
        call(registry, "interact", preview_id=pid)
    assert not backend.actions


def test_logs_are_allowlisted_even_on_failure(tmp_path, caplog, monkeypatch):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    caplog.set_level(logging.INFO)
    secret = "synthetic-secret-never-log"
    pid = preview(registry, wid, operation="text", text=secret)
    approve(adapter, run, pid)
    assert call(registry, "interact", preview_id=pid)["verified"]
    backend.hook = lambda stage: (_ for _ in ()).throw(RuntimeError(secret))
    assert call(registry, "discover", app_id="fixture")["code"] == "desktop_failed"
    assert secret not in caplog.text and str(app.executable) not in caplog.text
    assert "desktop_preview_created" in caplog.text
    backend.hook = lambda stage: None
    pid = preview(registry, wid)
    monkeypatch.setattr(
        logging.Logger,
        "info",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    with pytest.raises(PermissionDenied):
        call(registry, "interact", preview_id=pid)
    assert backend.actions == ["text"]


def test_executable_change_after_approval_is_rejected(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    app.executable.write_bytes(b"replacement binary")
    assert call(registry, "interact", preview_id=pid)["code"] == "target_mismatch"
    assert not backend.actions


def test_unknown_controls_operations_and_preview_limit(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path, max_pending=1)
    for args in (
        {"operation": "hotkey"},
        {"operation": "click", "control": "unknown"},
        {"operation": "focus", "text": "extra"},
        {"operation": "launch", "window_id": wid},
    ):
        result = call(
            registry, "preview", **({"app_id": "fixture", "window_id": wid} | args)
        )
        assert result["status"] == "error"
    pid = preview(registry, wid)
    assert (
        call(registry, "preview", app_id="fixture", operation="focus", window_id=wid)[
            "code"
        ]
        == "preview_limit"
    )
    adapter.discard(pid)
    assert call(registry, "interact", preview_id=pid)["code"] == "preview_not_found"


def test_control_configuration_requires_benign_postcondition():
    with pytest.raises(DesktopError):
        ControlRule("button", "1", "Button", "click")
    with pytest.raises(DesktopError):
        ControlRule("password", "1", "Password", "text")
