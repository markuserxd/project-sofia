import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from workspace_helpers import allow, call, setup

from sofia.security import Action, PermissionDenied
from sofia.tools import WorkspaceConfig, WorkspaceError, WorkspaceFiles
from sofia.tools.workspace_paths import check_type, relative_parts


@pytest.mark.parametrize(
    "path",
    [
        "",
        "..",
        "../x",
        "a/../b",
        "..\\x",
        "/etc/passwd",
        "C:note.txt",
        "C:/x",
        "\\\\server\\share",
        "\\\\?\\C:\\x",
        "\\\\.\\NUL",
        "a:b",
        "a/",
        "a//b",
        "a/./b",
        "a\\b/c",
        "./x",
        "a\0b",
        "a\nb",
        "a\x7fb",
        "NUL",
        "CON .txt",
        "con.txt",
        "AUX",
        "LPT1.txt",
        "COM¹.log",
        "CONIN$",
        "x.",
        "x ",
        " x",
        "a/*",
        "a/?",
        "a/<b",
        "LONGFI~1.TXT",
        "a" * 241,
    ],
)
def test_dangerous_paths_rejected_before_access(path):
    with pytest.raises(WorkspaceError, match="invalid_path"):
        relative_parts(path)


def test_relative_paths_and_root_listing():
    assert relative_parts("folder/hello world.txt") == ("folder", "hello world.txt")
    assert relative_parts(".", directory=True) == ()
    assert relative_parts("日本語.txt") == ("日本語.txt",)


@pytest.mark.parametrize("kind", ["missing", "file", "drive", "home", "relative"])
def test_bad_root(tmp_path, kind):
    _, _, run = setup(tmp_path)
    root = {
        "missing": tmp_path / "missing",
        "file": tmp_path / "file",
        "drive": Path(tmp_path.anchor),
        "home": Path.home(),
        "relative": Path("relative"),
    }[kind]
    if kind == "file":
        root.write_text("data")
    with pytest.raises(WorkspaceError):
        WorkspaceFiles(WorkspaceConfig(root), run=run)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": 8193},
        {"max_entries": 65},
        {"max_entries": True},
        {"exclusive_access": "true"},
    ],
)
def test_invalid_configuration(tmp_path, kwargs):
    with pytest.raises(WorkspaceError, match="invalid_workspace"):
        WorkspaceConfig(tmp_path, **kwargs)


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".env",
        ".env.example",
        ".venv/x",
        "credentials.json",
        "id_rsa",
        "key.pem",
        "secret.txt",
        "token.json",
    ],
)
def test_sensitive_names_denied(tmp_path, path):
    files, registry, run = setup(tmp_path)
    run.permissions.approve(Action.SENSITIVE_READ, files.permission_scope)
    assert call(registry, "read", path=path)["code"] == "sensitive_file"


def test_unexpected_reparse_attribute_blocked_deterministically():
    with pytest.raises(WorkspaceError, match="link_blocked"):
        check_type(
            SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400, st_nlink=1)
        )


def test_root_not_model_argument(tmp_path):
    files, registry, _ = setup(tmp_path)
    assert (
        registry.execute("workspace_read", {"path": "x", "root": str(tmp_path)}).error
        == "invalid_arguments"
    )
    assert not registry.contains("workspace_register")
    assert registry.execute("workspace_approve", {}).error == "unknown_tool"
    assert str(tmp_path) not in repr(files) + repr(WorkspaceConfig(tmp_path))


def test_separate_roots_and_scope_isolation(tmp_path):
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace-other"
    root.mkdir()
    sibling.mkdir()
    (root / "note.txt").write_text("one")
    (sibling / "note.txt").write_text("two")
    files, registry, run = setup(root)
    second = WorkspaceFiles(WorkspaceConfig(sibling), run=run)
    second.register(registry, prefix="second")
    allow(files, run, "note.txt")
    assert call(registry, "read", path="note.txt")["content"] == "one"
    with pytest.raises(PermissionDenied):
        registry.execute("second_read", {"path": "note.txt"})
    assert (
        call(registry, "read", path=str(sibling / "note.txt"))["code"] == "invalid_path"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows drive and case handling")
def test_windows_case_and_drive_forms(tmp_path):
    files, registry, run = setup(tmp_path)
    (tmp_path / "Note.txt").write_text("data")
    allow(files, run, "Note.txt")
    assert call(registry, "read", path="Note.txt")["content"] == "data"
    # Permission spellings are exact, even on a case-insensitive volume.
    with pytest.raises(PermissionDenied):
        call(registry, "read", path="note.txt")
    for path in ("D:x", "D:/x", "//server/share", "note.txt:stream"):
        assert call(registry, "read", path=path)["code"] == "invalid_path"
