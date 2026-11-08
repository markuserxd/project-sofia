import json
import logging
import os
from unittest.mock import Mock

import pytest
from workspace_helpers import allow, approve, call, mutate, preview, setup

from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    StepLimitExceeded,
)
from sofia.tools import WorkspaceConfig, WorkspaceError, WorkspaceFiles
from sofia.tools.workspace_paths import Directory, walk_directory


def symlink(target, link, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip(
                "Windows symlink privilege unavailable (WinError 1314); junction and reparse-attribute tests still run"
            )
        raise


@pytest.mark.parametrize(
    "kind",
    [
        "outside_file",
        "inside_file",
        "dangling",
        "outside_directory",
        "inside_directory",
        "nested",
    ],
)
def test_links_rejected(tmp_path, kind):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "note.txt").write_text("external")
    (root / "inside").mkdir()
    (root / "inside" / "note.txt").write_text("internal")
    link = root / "link"
    directory = "directory" in kind or kind == "nested"
    target = outside if directory else outside / "note.txt"
    if kind.startswith("inside"):
        target = root / "inside" if directory else root / "inside" / "note.txt"
    if kind == "dangling":
        target = outside / "missing"
    if kind == "nested":
        symlink(outside, root / "first", directory=True)
        target = root / "first"
    symlink(target, link, directory=directory)
    files, registry, run = setup(root)
    path = "link/note.txt" if directory else "link"
    allow(files, run, path)
    assert call(registry, "read", path=path)["status"] == "error"
    for operation in ("replace", "delete"):
        args = {"path": path, "operation": operation}
        if operation == "replace":
            args["content"] = "bad"
        assert call(registry, "preview", **args)["status"] == "error"
    allow(files, run, ".", directory=True)
    assert all(
        entry["path"] not in ("link", "first")
        for entry in call(registry, "list", path=".")["entries"]
    )
    assert (outside / "note.txt").read_text() == "external"


def test_link_root_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    symlink(root, tmp_path / "link", directory=True)
    _, _, run = setup(root)
    with pytest.raises(WorkspaceError):
        WorkspaceFiles(WorkspaceConfig(tmp_path / "link"), run=run)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows junction test")
def test_windows_junctions_and_pinned_parent(tmp_path):
    import _winapi

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "note.txt").write_text("external")
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    files, registry, run = setup(root)
    allow(files, run, "junction/note.txt")
    assert call(registry, "read", path="junction/note.txt")["code"] == "link_blocked"
    with pytest.raises(WorkspaceError, match="link_blocked"):
        WorkspaceFiles(WorkspaceConfig(root / "junction"), run=run)
    (root / "folder").mkdir()
    with walk_directory(root, ("folder",), files._identity, lambda: None):
        with pytest.raises(PermissionError):
            (root / "folder").rename(root / "moved")
    assert (outside / "note.txt").read_text() == "external"


@pytest.mark.parametrize("operation", ["read", "replace", "delete"])
def test_hard_link_aliases_blocked(tmp_path, operation):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "external.txt"
    outside.write_text("private")
    os.link(outside, root / "alias.txt")
    files, registry, run = setup(root)
    allow(files, run, "alias.txt")
    args = {"path": "alias.txt"}
    tool = "read"
    if operation != "read":
        tool = "preview"
        args["operation"] = operation
        if operation == "replace":
            args["content"] = "bad"
    assert call(registry, tool, **args)["code"] == "hard_link_blocked"
    assert outside.read_text() == "private"
    assert (root / "alias.txt").read_text() == "private"


def test_hard_link_added_after_approval(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.txt").write_text("original")
    files, registry, run = setup(root)
    pid = preview(files, registry, run, operation="replace")
    approve(files, run, pid)
    os.link(root / "note.txt", tmp_path / "alias.txt")
    assert call(registry, "replace", preview_id=pid)["code"] == "stale_preview"
    assert (tmp_path / "alias.txt").read_text() == "original"


def test_parent_and_root_identity_changes_fail(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "folder").mkdir()
    files, registry, run = setup(root)
    pid = preview(files, registry, run, path="folder/note.txt")
    approve(files, run, pid)
    (root / "folder").rename(root / "old")
    (root / "folder").mkdir()
    assert call(registry, "create", preview_id=pid)["code"] == "stale_preview"
    allow(files, run, ".", directory=True)
    if os.name == "nt":
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "old-root")
    else:
        root.rename(tmp_path / "old-root")
        root.mkdir()
        assert call(registry, "list", path=".")["code"] == "workspace_changed"


def test_close_releases_root_and_disables_registered_tools(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    files, registry, run = setup(root)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    scope = files.approval_scope(pid)
    files.close()
    files.close()
    with pytest.raises(PermissionDenied):
        run.permissions.check(Action.FILE_WRITE, scope)
    assert call(registry, "create", preview_id=pid)["code"] == "workspace_closed"
    root.rename(tmp_path / "moved")


def test_parent_replaced_by_link_after_preview(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "folder").mkdir()
    files, registry, run = setup(root)
    pid = preview(files, registry, run, path="folder/note.txt")
    approve(files, run, pid)
    (root / "folder").rename(root / "old")
    symlink(outside, root / "folder", directory=True)
    assert call(registry, "create", preview_id=pid)["status"] == "error"
    assert not list(outside.iterdir())


@pytest.mark.parametrize("operation", ["create", "replace"])
def test_final_target_change_before_commit(tmp_path, monkeypatch, operation):
    files, registry, run = setup(tmp_path)
    target = tmp_path / "note.txt"
    if operation == "replace":
        target.write_text("original")
    pid = preview(files, registry, run, operation=operation)
    approve(files, run, pid)
    flush = os.fsync

    def change(fd):
        flush(fd)
        target.write_text("concurrent")

    monkeypatch.setattr(os, "fsync", change)
    assert call(registry, operation, preview_id=pid)["code"] == "stale_preview"
    assert target.read_text() == "concurrent"
    assert not list(tmp_path.glob(".sofia-*"))


def test_exclusive_publication_rejects_last_instant_destination(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)
    publish = Directory.publish

    def appearance(parent, temporary, name, *, replace):
        (tmp_path / name).write_text("appeared")
        publish(parent, temporary, name, replace=replace)

    monkeypatch.setattr(Directory, "publish", appearance)
    assert mutate(files, registry, run)["code"] == "already_exists"
    assert (tmp_path / "note.txt").read_text() == "appeared"
    assert not list(tmp_path.glob(".sofia-*"))


@pytest.mark.parametrize(
    "failure", ["partial", "fsync", "publish", "unexpected", "interrupt"]
)
def test_atomic_failure_preserves_original(tmp_path, monkeypatch, failure):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_text("original")
    pid = preview(files, registry, run, operation="replace", content="replacement")
    approve(files, run, pid)

    def fail(*args, **kwargs):
        if failure == "partial":
            original_write(args[0], args[1][:2])
        if failure == "unexpected":
            raise RuntimeError("private-error")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        raise OSError("private-error")

    original_write = os.write
    if failure == "publish":
        monkeypatch.setattr(Directory, "publish", fail)
    else:
        monkeypatch.setattr(os, "fsync" if failure == "fsync" else "write", fail)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            registry.execute("workspace_replace", {"preview_id": pid})
    else:
        result = registry.execute("workspace_replace", {"preview_id": pid})
        assert (
            result.error == "handler_failed"
            if failure == "unexpected"
            else result.output["code"] == "filesystem_error"
        )
    assert (tmp_path / "note.txt").read_text() == "original"
    assert not list(tmp_path.glob(".sofia-*"))
    assert call(registry, "replace", preview_id=pid)["code"] == "preview_not_found"


def test_temp_collision_does_not_delete_existing_file(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)

    def collision(parent, name):
        (tmp_path / name).write_text("preexisting")
        raise FileExistsError()

    monkeypatch.setattr(Directory, "create", collision)
    assert mutate(files, registry, run)["code"] == "already_exists"
    assert [p.read_text() for p in tmp_path.glob(".sofia-*")] == ["preexisting"]
    assert not (tmp_path / "note.txt").exists()


def test_cleanup_failure_is_reported_without_false_rollback(
    tmp_path, monkeypatch, caplog
):
    files, registry, run = setup(tmp_path)
    original = Directory.unlink

    def fail(parent, name):
        if name.startswith(".sofia-"):
            raise OSError("private-temp-path")
        original(parent, name)

    monkeypatch.setattr(Directory, "unlink", fail)
    with caplog.at_level(logging.INFO, logger="sofia"):
        result = mutate(files, registry, run)
    assert result["committed"] and result["cleanup_failed"]
    assert (tmp_path / "note.txt").read_text() == "hello"
    assert "private-temp-path" not in caplog.text


@pytest.mark.parametrize("stop", ["cancel", "timeout", "revoke", "accounting"])
def test_stop_before_commit_preserves_original(tmp_path, monkeypatch, stop):
    files, registry, run = setup(tmp_path)
    now = [0.0]
    registry._clock = lambda: now[0]
    (tmp_path / "note.txt").write_text("original")
    pid = preview(files, registry, run, operation="replace")
    approve(files, run, pid)
    scope = files.approval_scope(pid)
    flush = os.fsync

    def stop_now(fd):
        flush(fd)
        if stop == "cancel":
            run.cancellation.cancel()
        elif stop == "timeout":
            now[0] = 10
        elif stop == "revoke":
            run.permissions.revoke(Action.FILE_WRITE, scope)
        else:
            from decimal import Decimal

            reservation = run.budget.reserve(
                estimated_tokens=0, estimated_usd=Decimal(0)
            )
            with pytest.raises(AccountingBlocked):
                run.budget.settle(reservation, actual_tokens=None, actual_usd=None)

    monkeypatch.setattr(os, "fsync", stop_now)
    if stop == "timeout":
        assert (
            registry.execute("workspace_replace", {"preview_id": pid}).error
            == "timeout"
        )
    else:
        exception = {
            "cancel": OperationCancelled,
            "revoke": PermissionDenied,
            "accounting": AccountingBlocked,
        }[stop]
        with pytest.raises(exception):
            registry.execute("workspace_replace", {"preview_id": pid})
    assert (tmp_path / "note.txt").read_text() == "original"
    assert not list(tmp_path.glob(".sofia-*"))


def test_steps_and_cancellation_before_dispatch(tmp_path):
    files, registry, run = setup(tmp_path, steps=2)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    assert call(registry, "create", preview_id=pid)["committed"]
    assert run.budget.snapshot.steps == 2
    with pytest.raises(StepLimitExceeded):
        call(registry, "read", path="note.txt")
    run.cancellation.cancel()
    with pytest.raises(OperationCancelled):
        call(registry, "read", path="note.txt")


def test_late_cancel_reports_commit_without_claiming_rollback(
    tmp_path, monkeypatch, caplog
):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    publish = Directory.publish

    def late(parent, temporary, name, *, replace):
        publish(parent, temporary, name, replace=replace)
        run.cancellation.cancel()

    monkeypatch.setattr(Directory, "publish", late)
    with (
        caplog.at_level(logging.INFO, logger="sofia"),
        pytest.raises(OperationCancelled),
    ):
        call(registry, "create", preview_id=pid)
    assert (tmp_path / "note.txt").read_text() == "hello"
    event = next(
        json.loads(r.message) for r in caplog.records if r.name == "sofia.workspace"
    )
    assert event["committed"]


def test_audit_and_logging_failure_do_not_leak_or_authorize(
    tmp_path, caplog, monkeypatch
):
    files, registry, run = setup(tmp_path)
    with caplog.at_level(logging.INFO, logger="sofia"):
        assert mutate(
            files, registry, run, path="private-name.txt", content="private-payload"
        )["committed"]
    assert "private-name" not in caplog.text and "private-payload" not in caplog.text
    assert str(tmp_path) not in caplog.text and ".sofia-" not in caplog.text
    assert any(r.name == "sofia.tools" for r in caplog.records)
    monkeypatch.setattr(
        logging.getLogger("sofia.workspace"),
        "info",
        Mock(side_effect=RuntimeError("private")),
    )
    assert mutate(files, registry, run, path="second.txt")["committed"]
    run.permissions.revoke(Action.FILE_WRITE, files.permission_scope)
    pid = preview(files, registry, run, path="third.txt")
    with pytest.raises(PermissionDenied):
        call(registry, "create", preview_id=pid)
    assert not (tmp_path / "third.txt").exists()


def test_registration_run_mismatch_and_duplicate(tmp_path):
    files, registry, run = setup(tmp_path)
    with pytest.raises(WorkspaceError, match="invalid_registration"):
        files.register(registry)
    _, other_registry, _ = setup(tmp_path)
    with pytest.raises(WorkspaceError, match="invalid_registration"):
        files.register(other_registry, prefix="second")
    assert registry.run_guardrails is run


@pytest.mark.skipif(os.name != "nt", reason="Native NTFS named-stream fixture")
def test_windows_named_data_streams_cannot_be_silently_lost(tmp_path):
    files, registry, run = setup(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("ordinary text")
    with open(str(target) + ":extra", "w") as stream:
        stream.write("additional data")
    allow(files, run, "note.txt")
    assert call(registry, "read", path="note.txt")["code"] == "unsupported_file_type"
    assert (
        call(registry, "preview", path="note.txt", operation="delete")["code"]
        == "unsupported_file_type"
    )
    with open(str(target) + ":extra") as stream:
        assert stream.read() == "additional data"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DACL preservation")
def test_windows_replacement_preserves_restrictive_dacl(tmp_path):
    import ctypes
    from ctypes import wintypes

    files, registry, run = setup(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("original")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    convert.restype = wintypes.BOOL
    set_security = security.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    get = security.GetFileSecurityW
    get.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    assert convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None)
    try:
        assert set_security(str(target), 4 | 0x80000000, descriptor)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(descriptor)

    def descriptor_bytes():
        needed = wintypes.DWORD()
        get(str(target), 4, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        assert get(str(target), 4, buffer, len(buffer), ctypes.byref(needed))
        return buffer.raw

    before = descriptor_bytes()
    assert mutate(files, registry, run, operation="replace")["committed"]
    assert descriptor_bytes() == before
    assert target.read_text() == "hello"


def test_permission_copy_failure_preserves_original(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_text("original")
    monkeypatch.setattr(
        Directory,
        "copy_permissions",
        Mock(side_effect=WorkspaceError("metadata_unsupported")),
    )
    assert (
        mutate(files, registry, run, operation="replace")["code"]
        == "metadata_unsupported"
    )
    assert (tmp_path / "note.txt").read_text() == "original"
    assert not list(tmp_path.glob(".sofia-*"))


def test_file_disappears_or_permission_changes_before_open(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_text("original")
    allow(files, run, "note.txt")
    for error, code in (
        (FileNotFoundError(), "not_found"),
        (PermissionError("private"), "filesystem_denied"),
    ):
        monkeypatch.setattr(Directory, "open_read", Mock(side_effect=error))
        assert call(registry, "read", path="note.txt")["code"] == code


def test_final_file_swapped_to_symlink_between_stat_and_open(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be disclosed")
    target = root / "note.txt"
    target.write_text("inside")
    # Establish privilege availability before invoking a handler (pytest skip is
    # not an ordinary filesystem failure and must not get swallowed by registry).
    symlink(outside, root / "probe")
    (root / "probe").unlink()
    files, registry, run = setup(root)
    allow(files, run, "note.txt")
    stat_file = Directory.stat

    def swap(parent, name):
        info = stat_file(parent, name)
        target.unlink()
        target.symlink_to(outside)
        return info

    monkeypatch.setattr(Directory, "stat", swap)
    result = call(registry, "read", path="note.txt")
    assert result["status"] == "error"
    assert "must not be disclosed" not in repr(result)
    assert outside.read_text() == "must not be disclosed"


@pytest.mark.parametrize("tool", ["read", "list"])
def test_cancellation_during_bounded_read_or_listing(tmp_path, monkeypatch, tool):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_text("x" * 5000)
    if tool == "read":
        allow(files, run, "note.txt")
        read = os.read

        def stop_read(fd, count):
            data = read(fd, count)
            run.cancellation.cancel()
            return data

        monkeypatch.setattr(os, "read", stop_read)
        path = "note.txt"
    else:
        allow(files, run, ".", directory=True)
        stat_file = Directory.stat

        def stop_stat(parent, name):
            info = stat_file(parent, name)
            run.cancellation.cancel()
            return info

        monkeypatch.setattr(Directory, "stat", stop_stat)
        path = "."
    with pytest.raises(OperationCancelled):
        call(registry, tool, path=path)
    assert (tmp_path / "note.txt").stat().st_size == 5000


def test_unrecognized_workspace_error_is_sanitized(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)
    allow(files, run, "note.txt")
    monkeypatch.setattr(
        Directory, "open_read", Mock(side_effect=WorkspaceError("private-error"))
    )
    assert call(registry, "read", path="note.txt")["code"] == "filesystem_error"


def test_delete_failure_preserves_file_and_consumes_preview(tmp_path, monkeypatch):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_text("original")
    pid = preview(files, registry, run, operation="delete")
    approve(files, run, pid)
    monkeypatch.setattr(
        Directory, "unlink", Mock(side_effect=PermissionError("private"))
    )
    assert call(registry, "delete", preview_id=pid)["code"] == "filesystem_denied"
    assert (tmp_path / "note.txt").read_text() == "original"
    assert call(registry, "delete", preview_id=pid)["code"] == "preview_not_found"


@pytest.mark.skipif(
    os.name != "posix", reason="Linux mount-ID and remote-filesystem checks"
)
def test_linux_bind_mount_and_network_filesystem_checks(tmp_path, monkeypatch):
    from io import StringIO

    from sofia.tools import workspace_paths

    files, registry, run = setup(tmp_path)
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "note.txt").write_text("not permitted")
    allow(files, run, "folder/note.txt")
    mount_id = workspace_paths._mount_id

    def different_mount(fd):
        return (
            -1
            if os.readlink(f"/proc/self/fd/{fd}").endswith("/folder")
            else mount_id(fd)
        )

    monkeypatch.setattr(workspace_paths, "_mount_id", different_mount)
    assert call(registry, "read", path="folder/note.txt")["code"] == "volume_blocked"
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: StringIO("1 0 0:1 / / rw - nfs server:/share rw\n"),
    )
    with pytest.raises(WorkspaceError, match="unsupported_filesystem"):
        workspace_paths._check_local_posix(tmp_path)
