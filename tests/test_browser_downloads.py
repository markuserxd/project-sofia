from pathlib import Path

import pytest
from browser_helpers import approve, call, grant, setup

from examples.browser_test_server import DOWNLOAD, fixture_policy
from sofia.security import Action, PermissionDenied
from sofia.tools.browser import BrowserAutomation
from sofia.tools.browser_policy import BrowserConfig, BrowserError


def test_no_implicit_quarantine_or_download():
    env = setup(fixture_policy("http://127.0.0.1:8123"))
    adapter, registry, run, backend, sid = env
    try:
        grant(adapter, run, "preview_download", session=sid, target="text")
        assert (
            call(registry, "preview_download", session_id=sid, target="text")["code"]
            == "download_disabled"
        )
        assert "download" not in backend.actions
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "denied",
        "wrong_scope",
        "overwrite",
        "wrong_body",
        "write_failure",
        "changed_link",
        "cancel",
    ],
)
def test_quarantine_permissions_and_exclusive_publication(tmp_path, monkeypatch, case):
    env = setup(fixture_policy("http://127.0.0.1:8123"), quarantine=tmp_path)
    adapter, registry, run, backend, sid = env
    try:
        grant(adapter, run, "preview_download", session=sid, target="text")
        pid = call(registry, "preview_download", session_id=sid, target="text")[
            "preview_id"
        ]
        proposal = adapter.proposal(pid)
        assert not list(tmp_path.iterdir()) and "download" not in backend.actions
        grant(adapter, run, "download", session=sid, target="text")
        if case in ("denied", "wrong_scope"):
            if case == "wrong_scope":
                run.permissions.approve(
                    Action.FILE_WRITE, adapter.permission_scope + ":path:foreign"
                )
            with pytest.raises(PermissionDenied):
                call(registry, "download", preview_id=pid)
            assert not list(tmp_path.iterdir())
            return
        approve(adapter, run, pid)
        if case == "overwrite":
            (tmp_path / proposal.destination).write_text("existing", encoding="utf-8")
        elif case == "wrong_body":
            backend.download = lambda *a: b"x" * 100000
        elif case == "write_failure":
            monkeypatch.setattr("sofia.tools.browser.os.write", lambda *a: 0)
        elif case == "changed_link":
            backend.download_state = lambda *a: "different link"
        elif case == "cancel":

            def stopped(*a):
                run.cancellation.cancel()
                return DOWNLOAD

            backend.download = stopped
        if case == "cancel":
            from sofia.security import OperationCancelled

            with pytest.raises(OperationCancelled):
                call(registry, "download", preview_id=pid)
        else:
            output = call(registry, "download", preview_id=pid)
            assert output["status"] == ("ok" if case == "success" else "error")
        files = list(tmp_path.iterdir())
        if case == "success":
            assert files == [tmp_path / proposal.destination]
            assert files[0].read_bytes() == DOWNLOAD
            assert call(registry, "download", preview_id=pid)["status"] == "error"
        elif case == "overwrite":
            assert files[0].read_text(encoding="utf-8") == "existing"
        else:
            assert not files
    finally:
        adapter.close()


def test_quarantine_requires_exclusive_controller_assertion(tmp_path):
    from decimal import Decimal

    from sofia.security import RunGuardrails

    run = RunGuardrails(max_steps=5, max_tokens=0, max_usd=Decimal(0))
    with pytest.raises(BrowserError, match="exclusive_access_required"):
        BrowserAutomation(BrowserConfig(), run=run, quarantine=Path(tmp_path))
