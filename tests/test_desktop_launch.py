from dataclasses import replace

import pytest
from desktop_helpers import approve, call, preview, setup

from sofia.security import OperationCancelled, PermissionDenied
from sofia.tools.desktop_backend import DesktopError


def launch_preview(tmp_path, **config):
    state = setup(tmp_path, **config)
    adapter, registry, run, backend, app, wid, clock = state
    backend.rows.clear()
    pid = preview(registry, wid, operation="launch")
    return state, pid


def test_launch_requires_exact_approval_and_verifies_readiness(tmp_path):
    (adapter, registry, run, backend, app, wid, clock), pid = launch_preview(tmp_path)
    with pytest.raises(PermissionDenied):
        call(registry, "launch", preview_id=pid)
    assert backend.launches == 0
    approve(adapter, run, pid)
    before = run.budget.snapshot.steps
    result = call(registry, "launch", preview_id=pid)
    assert result["verified"] and result["window_id"]
    assert run.budget.snapshot.steps == before + 1
    assert backend.launches == 1
    assert call(registry, "launch", preview_id=pid)["code"] == "preview_not_found"
    assert backend.launches == 1


@pytest.mark.parametrize(
    "failure,code",
    [
        ("never_ready", "timeout"),
        ("exited", "launch_failed"),
        ("wrong_window", "target_mismatch"),
        ("exception", "desktop_failed"),
        ("denied", "launch_failed"),
    ],
)
def test_launch_failures_are_bounded_and_not_retried(tmp_path, failure, code):
    (adapter, registry, run, backend, app, wid, clock), pid = launch_preview(
        tmp_path, readiness_attempts=3
    )
    approve(adapter, run, pid)
    if failure == "never_ready":
        backend.ready = False
    elif failure == "exited":
        backend.running = False
    elif failure == "wrong_window":
        backend.hook = lambda stage: (
            setattr(backend, "rows", [replace(backend.rows[0], window_class="other")])
            if stage == "alive"
            else None
        )
    else:

        def fail(stage):
            if stage == "launch":
                raise (
                    DesktopError("launch_failed")
                    if failure == "denied"
                    else RuntimeError("private exception")
                )

        backend.hook = fail
    assert call(registry, "launch", preview_id=pid)["code"] == code
    assert backend.launches <= 1
    assert backend.calls.count("alive") <= 3
    assert call(registry, "launch", preview_id=pid)["code"] == "preview_not_found"


def test_existing_instance_and_ambiguous_readiness_rejected(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid, operation="launch")
    approve(adapter, run, pid)
    assert call(registry, "launch", preview_id=pid)["code"] == "already_running"
    assert backend.launches == 0
    backend.rows.clear()
    pid = preview(registry, wid, operation="launch")
    approve(adapter, run, pid)
    backend.hook = lambda stage: (
        backend.rows.append(replace(backend.rows[0], handle=11))
        if stage == "alive"
        else None
    )
    assert call(registry, "launch", preview_id=pid)["code"] == "ambiguous_target"
    assert backend.launches == 1


@pytest.mark.parametrize("stage", ["before", "launch", "alive", "windows"])
def test_cancellation_during_startup(tmp_path, stage):
    (adapter, registry, run, backend, app, wid, clock), pid = launch_preview(tmp_path)
    approve(adapter, run, pid)
    if stage == "before":
        run.cancellation.cancel()
    else:
        backend.hook = lambda current: (
            run.cancellation.cancel() if current == stage else None
        )
    with pytest.raises(OperationCancelled):
        call(registry, "launch", preview_id=pid)
    assert backend.launches <= 1


def test_deadline_expiry_does_not_retry(tmp_path):
    (adapter, registry, run, backend, app, wid, clock), pid = launch_preview(tmp_path)
    approve(adapter, run, pid)
    backend.hook = lambda stage: clock.__setitem__(0, 10) if stage == "alive" else None
    result = registry.execute("desktop_launch", {"preview_id": pid})
    assert result.error == "timeout" and backend.launches == 1
