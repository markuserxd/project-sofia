"""Conservative cross-platform names and non-following directory traversal.

These primitives reduce path races; they are not a sandbox against local writers.
Mutations require exclusive local access asserted by the trusted controller.
"""

import os
import re
import stat
import sys
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


class WorkspaceError(Exception):
    """Only internal fixed codes; never include OS exception text."""

    _codes = frozenset(
        {
            "already_exists",
            "approval_mismatch",
            "binary_blocked",
            "concurrent_modification",
            "entry_limit",
            "exclusive_access_required",
            "hard_link_blocked",
            "invalid_encoding",
            "invalid_operation",
            "invalid_path",
            "invalid_registration",
            "invalid_workspace",
            "link_blocked",
            "metadata_unsupported",
            "preview_limit",
            "preview_not_found",
            "sensitive_file",
            "size_limit",
            "stale_preview",
            "unsupported_file_type",
            "unsupported_filesystem",
            "unsupported_platform",
            "volume_blocked",
            "workspace_changed",
            "workspace_closed",
            "write_failed",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = (
            code if type(code) is str and code in self._codes else "filesystem_error"
        )
        super().__init__(self.code)


def _mount_id(fd: int) -> int:
    # Linux mount identity also catches same-volume bind mounts, unlike st_dev.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as source:
        for line in source.read(4096).splitlines():
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise WorkspaceError("unsupported_filesystem")


def _check_local_posix(root: Path) -> None:
    if sys.platform != "linux":
        raise WorkspaceError("unsupported_platform")
    # Inspect the mount table before touching a potentially remote root.
    with open("/proc/self/mountinfo", encoding="utf-8") as source:
        data = source.read(1_048_577)
    if len(data) > 1_048_576:
        raise WorkspaceError("unsupported_filesystem")
    matches = []
    for line in data.splitlines():
        fields = line.split()
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if root.is_relative_to(mount):
            matches.append((len(mount.parts), fields[fields.index("-") + 1]))
    if not matches or max(matches)[1] not in {
        "ext4",
        "ext3",
        "ext2",
        "xfs",
        "btrfs",
        "tmpfs",
        "overlay",
    }:
        raise WorkspaceError("unsupported_filesystem")


def relative_parts(value: str, *, directory: bool = False) -> tuple[str, ...]:
    if directory and value == ".":
        return ()
    if (
        type(value) is not str
        or not value
        or len(value) > 240
        or any(unicodedata.category(c).startswith("C") for c in value)
        or any(c in value for c in '\\:<>"|?*~')
    ):
        raise WorkspaceError("invalid_path")
    parts = tuple(value.split("/"))
    for part in parts:
        if (
            part in ("", ".", "..")
            or part.endswith((" ", "."))
            or part.startswith(" ")
            or re.fullmatch(
                r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
                part.split(".")[0].rstrip(" "),
            )
        ):
            raise WorkspaceError("invalid_path")
    return parts


def private_parts(parts: tuple[str, ...]) -> bool:
    """Defense in depth, not a classifier for every possible secret."""
    return any(
        part.startswith(".")
        or part.casefold() in {"venv", "env", "node_modules", "__pycache__"}
        or any(word in part.casefold() for word in ("credential", "secret", "token"))
        or part.casefold().startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))
        or part.casefold().endswith((".pem", ".key", ".pfx", ".p12", ".kdbx"))
        for part in parts
    )


def identity(info: os.stat_result) -> tuple[int, int]:
    if not info.st_ino:
        raise WorkspaceError("unsupported_filesystem")
    return info.st_dev, info.st_ino


def check_type(info: os.stat_result, *, directory: bool = False) -> None:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkspaceError("link_blocked")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise WorkspaceError("unsupported_file_type")
    if not directory and info.st_nlink != 1:
        raise WorkspaceError("hard_link_blocked")


class Directory:
    """Live parent anchor. Windows parents are pinned against ordinary renames."""

    def __init__(self, path: Path, fd: int, device: int) -> None:
        self.path = path
        self.fd = fd
        self.device = device
        self.mount_id = _mount_id(fd) if os.name == "posix" else None

    def stat(self, name: str) -> os.stat_result:
        if os.name == "nt":
            return os.lstat(self.path / name)
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def open_read(self, name: str) -> int:
        before = self.stat(name)
        check_type(before)
        if os.name == "nt":
            from sofia.tools.workspace_windows import open_file

            fd = open_file(self.path / name, directory=False)
        else:
            fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW") | getattr(os, "O_NONBLOCK"),
                dir_fd=self.fd,
            )
        try:
            after = os.fstat(fd)
            check_type(after)
            if identity(before) != identity(after) or after.st_dev != self.device:
                raise WorkspaceError("concurrent_modification")
            if os.name == "posix" and _mount_id(fd) != self.mount_id:
                raise WorkspaceError("volume_blocked")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def create(self, name: str) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if os.name == "nt":
            return os.open(self.path / name, flags, 0o600)
        return os.open(name, flags | getattr(os, "O_NOFOLLOW"), 0o600, dir_fd=self.fd)

    def publish(self, temporary: str, name: str, *, replace: bool) -> None:
        if os.name == "nt":
            # Windows rename rejects an existing destination; replace is explicit.
            operation = os.replace if replace else os.rename
            operation(self.path / temporary, self.path / name)
        elif replace:
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            # POSIX rename clobbers. link provides atomic no-clobber publication.
            os.link(
                temporary,
                name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )

    def copy_permissions(self, name: str, temporary: str) -> None:
        if os.name == "nt":
            from sofia.tools.workspace_windows import copy_dacl

            copy_dacl(self.path / name, self.path / temporary)

    def unlink(self, name: str) -> None:
        if os.name == "nt":
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self.fd)

    @contextmanager
    def entries(self) -> Iterator[Iterator[os.DirEntry[str]]]:
        with os.scandir(self.path if os.name == "nt" else self.fd) as entries:
            yield entries


@contextmanager
def walk_directory(
    root: Path,
    parts: tuple[str, ...],
    expected_root: tuple[int, int] | None,
    checkpoint: Callable[[], None],
) -> Iterator[tuple[Directory, tuple[tuple[int, int], ...]]]:
    """Walk from the volume anchor, checking every component without following.

    POSIX operations use dir_fd and O_NOFOLLOW. Windows uses OPEN_REPARSE_POINT
    handles without FILE_SHARE_DELETE for every parent; full names are used only
    while those handles remain open. No Path.resolve-only access decisions.
    """
    with ExitStack() as stack:
        path = Path(root.anchor)
        fd: int | None = None
        root_device = None
        root_mount = None
        ancestry = []
        components = ("", *root.parts[1:], *parts)
        root_index = len(root.parts) - 1
        for index, component in enumerate(components):
            checkpoint()
            path = path / component
            if os.name == "nt":
                from sofia.tools.workspace_windows import open_file

                child_fd = open_file(path, directory=True)
            elif os.name == "posix":
                child_fd = os.open(
                    component if fd is not None else str(path),
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY")
                    | getattr(os, "O_NOFOLLOW"),
                    dir_fd=fd,
                )
            else:
                raise WorkspaceError("unsupported_platform")
            stack.callback(os.close, child_fd)
            info = os.fstat(child_fd)
            check_type(info, directory=True)
            current = identity(info)
            fd = child_fd
            if index == root_index:
                if expected_root is not None and current != expected_root:
                    raise WorkspaceError("workspace_changed")
                root_device = info.st_dev
                if os.name == "posix":
                    root_mount = _mount_id(fd)
            if index >= root_index:
                if root_device != info.st_dev:
                    raise WorkspaceError("volume_blocked")
                if os.name == "posix" and _mount_id(fd) != root_mount:
                    raise WorkspaceError("volume_blocked")
                ancestry.append(current)
        assert fd is not None and root_device is not None
        checkpoint()
        yield Directory(path, fd, root_device), tuple(ancestry)


def configured_root(root: Path) -> tuple[Path, tuple[int, int]]:
    try:
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise WorkspaceError("invalid_workspace")
        if os.name == "nt":
            from sofia.tools.workspace_windows import check_volume

            check_volume(root)
        elif os.name == "posix":
            _check_local_posix(root)
        else:
            raise WorkspaceError("unsupported_platform")
        # No implicit home, drive, or operating-system roots. Ancestors must also
        # be real directories; a root supplied through a link is not accepted.
        resolved = root.resolve(strict=True)
        forbidden = {Path(root.anchor), Path.home()}
        if os.name == "nt":
            forbidden.update(
                Path(root.anchor) / name
                for name in (
                    "Windows",
                    "Users",
                    "Program Files",
                    "Program Files (x86)",
                    "ProgramData",
                )
            )
        else:
            forbidden.update(
                Path(p)
                for p in (
                    "/etc",
                    "/usr",
                    "/bin",
                    "/sbin",
                    "/dev",
                    "/proc",
                    "/sys",
                    "/home",
                    "/root",
                    "/var",
                )
            )
        if resolved in forbidden:
            raise WorkspaceError("invalid_workspace")
        with walk_directory(root, (), None, lambda: None) as (directory, ancestry):
            if resolved != root or identity(os.stat(resolved)) != ancestry[0]:
                raise WorkspaceError("invalid_workspace")
            return resolved, ancestry[0]
    except (OSError, ValueError, RuntimeError):
        raise WorkspaceError("invalid_workspace") from None
