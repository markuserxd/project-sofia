"""Versioned Markdown and the private memory directory boundary."""

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from sofia.storage.models import MAX_TEXT, Draft, MemoryError, require_id
from sofia.tools.workspace_paths import Directory, check_type, identity, walk_directory

MAX_FILE = MAX_TEXT + 4096
DATABASE = "sofia_memory.db"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def default_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "SOFIA" / "memory"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "sofia" / "memory"


def _mkdir(parent: Directory, name: str) -> None:
    with suppress(FileExistsError):
        if os.name == "nt":
            (parent.path / name).mkdir(mode=0o700)
        else:
            os.mkdir(name, mode=0o700, dir_fd=parent.fd)


@contextmanager
def open_directory(path: Path, *, create: bool) -> Iterator[Directory]:
    if not path.is_absolute() or ".." in path.parts or path == path.parent:
        raise MemoryError("unsafe_path")
    if os.name == "nt":
        from sofia.tools.workspace_windows import check_volume

        check_volume(path)
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if not create:
                raise
            with walk_directory(component.parent, (), None, lambda: None) as (
                parent,
                _,
            ):
                _mkdir(parent, component.name)
            info = component.lstat()
        check_type(info, directory=True)
    with walk_directory(path, (), None, lambda: None) as (directory, _):
        yield directory


@contextmanager
def open_diary(root: Directory, *, create: bool) -> Iterator[Directory]:
    if create:
        _mkdir(root, "diary")
    # Use the root as the anchor so the directory must stay on its volume/mount.
    check_type(root.stat("diary"), directory=True)
    with walk_directory(
        root.path, ("diary",), identity(os.fstat(root.fd)), lambda: None
    ) as (files, _):
        yield files


def exists(directory: Directory, name: str) -> bool:
    try:
        check_type(directory.stat(name))
    except FileNotFoundError:
        return False
    return True


def sync(directory: Directory) -> None:
    if os.name == "posix":
        os.fsync(directory.fd)


def remove(directory: Directory, name: str) -> None:
    if exists(directory, name):
        directory.unlink(name)
        sync(directory)


def name_for(memory_id: str, version: int) -> str:
    require_id(memory_id)
    if type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise MemoryError("invalid_version")
    return f"{memory_id}.{version}.md"


def pending_for(memory_id: str) -> str:
    require_id(memory_id)
    return f"{memory_id}.pending"


def header(
    memory_id: str, draft: Draft, created: str, updated: str, version: int
) -> bytes:
    metadata = {
        "memory_id": memory_id,
        "entry_type": "diary",
        "created_at": created,
        "updated_at": updated,
        "version": version,
        "source": draft.source,
    }
    return f"---\n{json.dumps(metadata, ensure_ascii=False)}\n---\n\n# {draft.title}\n\n".encode()


def read(directory: Directory, name: str) -> bytes:
    if not exists(directory, name):
        raise MemoryError("diary_missing")
    with os.fdopen(directory.open_read(name), "rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise MemoryError("diary_invalid")
    return data


def stage(directory: Directory, memory_id: str, content: bytes) -> None:
    with os.fdopen(directory.create(pending_for(memory_id)), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    sync(directory)


def publish(directory: Directory, memory_id: str, version: int, expected: str) -> None:
    final, pending = name_for(memory_id, version), pending_for(memory_id)
    if exists(directory, final):
        # A stage that did not commit can be discarded only once the committed
        # version has been verified. A manually edited file is left for review.
        if digest(read(directory, final)) != expected:
            raise MemoryError("diary_changed")
        remove(directory, pending)
        return
    if digest(read(directory, pending)) != expected:
        raise MemoryError("diary_changed")
    directory.publish(pending, final, replace=True)
    sync(directory)
