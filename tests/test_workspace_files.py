import hashlib
import os

import pytest
from workspace_helpers import allow, approve, call, mutate, preview, setup

from sofia.security import Action, PermissionDenied
from sofia.tools import WorkspaceError


def test_create_list_read_replace_delete(tmp_path):
    files, registry, run = setup(tmp_path)
    assert mutate(files, registry, run)["committed"]
    allow(files, run, ".", directory=True)
    assert call(registry, "list", path=".")["entries"] == [
        {"path": "note.txt", "type": "file"}
    ]
    assert call(registry, "read", path="note.txt")["content"] == "hello"
    result = mutate(files, registry, run, operation="replace", content="new 🦉\n")
    assert result["digest"] == hashlib.sha256("new 🦉\n".encode()).hexdigest()
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "new 🦉\n"
    assert mutate(files, registry, run, operation="delete")["committed"]
    assert not (tmp_path / "note.txt").exists()
    assert not list(tmp_path.iterdir())


def test_preview_is_bounded_immutable_and_has_no_effect(tmp_path):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run, content="private-preview-content")
    proposal = files.proposal(pid)
    assert not list(tmp_path.iterdir())
    assert proposal.expected is None and proposal.content == "private-preview-content"
    assert "private-preview-content" not in repr(proposal)
    with pytest.raises(AttributeError):
        proposal.path = "other.txt"
    files.discard(pid)
    with pytest.raises(WorkspaceError, match="preview_not_found"):
        files.proposal(pid)


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_three_permission_scopes_required(tmp_path, operation):
    files, registry, run = setup(tmp_path)
    target = tmp_path / "note.txt"
    if operation != "create":
        target.write_text("original")
    pid = preview(files, registry, run, operation=operation)
    action = Action.FILE_DELETE if operation == "delete" else Action.FILE_WRITE
    with pytest.raises(PermissionDenied):
        call(registry, operation, preview_id=pid)
    run.permissions.approve(action, files.permission_scope)
    with pytest.raises(PermissionDenied):
        call(registry, operation, preview_id=pid)
    run.permissions.approve(action, files.path_scope("note.txt"))
    with pytest.raises(PermissionDenied):
        call(registry, operation, preview_id=pid)
    run.permissions.approve(action, files.approval_scope(pid))
    assert call(registry, operation, preview_id=pid)["committed"]
    assert call(registry, operation, preview_id=pid)["code"] == "preview_not_found"


@pytest.mark.parametrize("scope", ["workspace", "path", "preview"])
def test_revocation(tmp_path, scope):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    target = {
        "workspace": files.permission_scope,
        "path": files.path_scope("note.txt"),
        "preview": files.approval_scope(pid),
    }[scope]
    run.permissions.revoke(Action.FILE_WRITE, target)
    with pytest.raises(PermissionDenied):
        call(registry, "create", preview_id=pid)
    assert not (tmp_path / "note.txt").exists()


def test_wrong_content_target_operation_and_forged_claims(tmp_path):
    files, registry, run = setup(tmp_path)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    other_content = preview(files, registry, run, content="different")
    other_target = preview(files, registry, run, path="other.txt")
    for other in (other_content, other_target):
        with pytest.raises(PermissionDenied):
            call(registry, "create", preview_id=other)
    assert call(registry, "replace", preview_id=pid)["code"] == "approval_mismatch"
    for extra in (
        {"approved": True},
        {"path": "other.txt"},
        {"content": "different"},
        {"root": str(tmp_path)},
    ):
        assert (
            registry.execute("workspace_create", {"preview_id": pid, **extra}).error
            == "invalid_arguments"
        )
    assert call(registry, "create", preview_id="forged")["code"] == "preview_not_found"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
@pytest.mark.parametrize("change", ["content", "identity", "disappear"])
def test_stale_preview_consumed(tmp_path, operation, change):
    files, registry, run = setup(tmp_path)
    target = tmp_path / "note.txt"
    if operation != "create":
        target.write_text("original")
    pid = preview(files, registry, run, operation=operation)
    approve(files, run, pid)
    if operation == "create":
        target.write_text("appeared")
    elif change == "disappear":
        target.unlink()
    elif change == "identity":
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("original")
        replacement.replace(target)
    else:
        target.write_text("modified")
    before = target.read_bytes() if target.exists() else None
    assert call(registry, operation, preview_id=pid)["code"] == "stale_preview"
    assert (target.read_bytes() if target.exists() else None) == before
    assert call(registry, operation, preview_id=pid)["code"] == "preview_not_found"


def test_read_only_configuration_fails_closed_for_mutations(tmp_path):
    files, registry, run = setup(tmp_path, exclusive=False)
    pid = preview(files, registry, run)
    approve(files, run, pid)
    assert (
        call(registry, "create", preview_id=pid)["code"] == "exclusive_access_required"
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "data,code",
    [
        (b"\xff", "invalid_encoding"),
        (b"\x00", "binary_blocked"),
        (b"-----BEGIN PRIVATE KEY-----", "sensitive_file"),
        (b"x" * 8193, "size_limit"),
    ],
)
def test_read_rejects_unsafe_content(tmp_path, data, code):
    files, registry, run = setup(tmp_path)
    (tmp_path / "note.txt").write_bytes(data)
    allow(files, run, "note.txt")
    assert call(registry, "read", path="note.txt")["code"] == code


def test_utf8_bound_empty_and_invalid_write_content(tmp_path):
    files, registry, run = setup(tmp_path, max_bytes=4)
    assert mutate(files, registry, run, content="")["bytes"] == 0
    allow(files, run, "other.txt")
    for text, code in (
        ("🦉x", "size_limit"),
        ("\ud800", "invalid_encoding"),
        ("\0", "binary_blocked"),
    ):
        assert (
            call(
                registry, "preview", path="other.txt", operation="create", content=text
            )["code"]
            == code
        )


def test_listing_sorted_nonrecursive_and_private_entries_hidden(tmp_path):
    files, registry, run = setup(tmp_path)
    for name in ("z.txt", "a.txt", ".env", "credentials.json"):
        (tmp_path / name).write_text("not read")
    (tmp_path / "folder").mkdir()
    (tmp_path / "folder" / "nested.txt").write_text("not listed")
    allow(files, run, ".", directory=True)
    result = call(registry, "list", path=".")
    assert [entry["path"] for entry in result["entries"]] == [
        "a.txt",
        "folder",
        "z.txt",
    ]
    assert result["count"] == 3


def test_entry_limit_and_preview_limit(tmp_path):
    files, registry, run = setup(tmp_path, max_entries=2)
    for name in ("a", "b", ".env"):
        (tmp_path / name).touch()
    allow(files, run, ".", directory=True)
    assert call(registry, "list", path=".")["code"] == "entry_limit"
    ids = [preview(files, registry, run) for _ in range(32)]
    assert (
        call(registry, "preview", path="note.txt", operation="create", content="x")[
            "code"
        ]
        == "preview_limit"
    )
    files.discard(ids[0])
    assert preview(files, registry, run)


def test_not_found_directory_and_existing_creation(tmp_path):
    files, registry, run = setup(tmp_path)
    allow(files, run, "note.txt")
    assert call(registry, "read", path="note.txt")["code"] == "not_found"
    (tmp_path / "note.txt").mkdir()
    assert call(registry, "read", path="note.txt")["code"] == "unsupported_file_type"
    assert (
        call(registry, "preview", path="note.txt", operation="delete")["code"]
        == "unsupported_file_type"
    )
    assert (
        call(registry, "preview", path="note.txt", operation="create", content="x")[
            "code"
        ]
        == "already_exists"
    )
    assert not registry.contains("workspace_rmtree")


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX FIFO fixture; Windows device names covered by path tests",
)
def test_fifo_fails_without_opening_or_blocking(tmp_path):
    files, registry, run = setup(tmp_path)
    os.mkfifo(tmp_path / "pipe")
    allow(files, run, "pipe")
    assert call(registry, "read", path="pipe")["code"] == "unsupported_file_type"
