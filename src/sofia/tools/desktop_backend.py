"""Desktop boundary records and trusted configuration; no native imports or I/O on import."""

import hashlib
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sofia.tools.registry import ToolContext
from sofia.tools.schemas import valid_name


class DesktopError(ValueError):
    """Only fixed internal codes may cross the desktop tool boundary."""


@dataclass(frozen=True, slots=True, repr=False)
class FileIdentity:
    path: Path
    device: int
    inode: int
    size: int
    modified: int
    digest: str


def file_identity(path: Path) -> FileIdentity:
    """Reject aliases/reparse ancestors and fingerprint a bounded regular file."""
    try:
        if not isinstance(path, Path) or not path.is_absolute():
            raise DesktopError("invalid_executable")
        for part in (path, *path.parents):
            info = part.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise DesktopError("invalid_executable")
        canonical = path.resolve(strict=True)
        with canonical.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= 128 * 1024**2
            ):
                raise DesktopError("invalid_executable")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DesktopError("invalid_executable")
        return FileIdentity(
            canonical,
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            digest,
        )
    except (OSError, RuntimeError):
        raise DesktopError("invalid_executable") from None


@dataclass(frozen=True, slots=True)
class ControlRule:
    """Trusted benign direct-child selector and fixed postcondition, never model input."""

    key: str
    automation_id: str
    control_type: str
    operation: str
    status_id: str = ""
    expected_status: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if (
            not valid_name(self.key)
            or type(self.automation_id) is not str
            or not 1 <= len(self.automation_id) <= 128
            or (self.operation, self.control_type)
            not in {("click", "Button"), ("text", "Edit")}
            or type(self.status_id) is not str
            or len(self.status_id) > 128
            or type(self.expected_status) is not str
            or len(self.expected_status) > 128
            or (
                self.operation == "click"
                and (not self.status_id or not self.expected_status)
            )
        ):
            raise DesktopError("invalid_control")


_FIXTURE = object()
# Defense in depth. Trusted code must assess the semantics of every permitted app/control.
_UNSAFE = {
    "cmd",
    "powershell",
    "pwsh",
    "python",
    "pythonw",
    "py",
    "wscript",
    "cscript",
    "mshta",
    "rundll32",
    "regsvr32",
    "regedit",
    "mmc",
    "control",
    "msiexec",
    "setup",
    "install",
    "installer",
    "chrome",
    "msedge",
    "firefox",
    "iexplore",
    "credentialui",
    "keepass",
    "keepassxc",
    "1password",
    "bitwarden",
    "taskmgr",
}


@dataclass(frozen=True, slots=True, repr=False)
class DesktopApplication:
    app_id: str
    executable: Path
    window_class: str
    controls: tuple[ControlRule, ...] = ()
    arguments: tuple[str, ...] = ()
    multiple_instances: bool = False
    _fixture_token: object = field(default=None, repr=False)
    version: FileIdentity = field(init=False)
    fixture_version: FileIdentity | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if (
            not valid_name(self.app_id)
            or not isinstance(self.executable, Path)
            or self.executable.suffix.lower() != ".exe"
            or type(self.window_class) is not str
            or not 1 <= len(self.window_class) <= 128
            or type(self.controls) is not tuple
            or len(self.controls) > 16
            or any(type(c) is not ControlRule for c in self.controls)
            or len({c.key for c in self.controls}) != len(self.controls)
            or len({(c.automation_id, c.control_type) for c in self.controls})
            != len(self.controls)
            or type(self.arguments) is not tuple
            or len(self.arguments) > 16
            or any(
                type(a) is not str or len(a) > 1024 or any(ord(c) < 32 for c in a)
                for a in self.arguments
            )
            or type(self.multiple_instances) is not bool
        ):
            raise DesktopError("invalid_application")
        fixture = self._fixture_token is _FIXTURE
        stem = self.executable.stem.lower()
        if (
            stem in _UNSAFE or stem.startswith(("python", "powershell"))
        ) and not fixture:
            raise DesktopError("unsafe_target")
        object.__setattr__(self, "version", file_identity(self.executable))
        if fixture:
            object.__setattr__(
                self, "fixture_version", file_identity(Path(self.arguments[-1]))
            )

    def revalidate(self) -> None:
        if file_identity(self.executable) != self.version:
            raise DesktopError("target_mismatch")
        if (
            self.fixture_version
            and file_identity(self.fixture_version.path) != self.fixture_version
        ):
            raise DesktopError("target_mismatch")


def fixture_application() -> DesktopApplication:
    """Repository-only exception: exact interpreter + isolated fixed synthetic script.

    The real backend accepts ONLY a process it created for this exception, never
    arbitrary windows belonging to other Python interpreters.
    """
    script = Path(__file__).resolve().parents[3] / "examples" / "desktop_fixture.py"
    return DesktopApplication(
        "sofia-fixture",
        # Windows venv redirectors spawn a different PID. Use the fixed base
        # interpreter directly so child ownership and UI identity remain exact.
        Path(getattr(sys, "_base_executable", sys.executable)).resolve(),
        "SOFIA_Desktop_Fixture",
        (
            ControlRule("entry", "1001", "Edit", "text"),
            ControlRule("button", "1002", "Button", "click", "1003", "SOFIA count: 1"),
        ),
        ("-I", str(script)),
        _fixture_token=_FIXTURE,
    )


@dataclass(frozen=True, slots=True, repr=False)
class WindowInfo:
    handle: int
    pid: int
    created: str
    executable: Path
    window_class: str
    visible: bool = True
    enabled: bool = True
    minimized: bool = False
    runtime_id: tuple[int, ...] = ()

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.handle,
            self.pid,
            self.created,
            self.executable,
            self.window_class,
            self.runtime_id,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ControlInfo:
    runtime_id: tuple[int, ...]
    automation_id: str
    control_type: str
    enabled: bool = True
    visible: bool = True
    password: bool = False
    editable: bool = False


class DesktopBackend(Protocol):
    """Trusted synchronous implementations must checkpoint and bound native work."""

    def session(self, context: ToolContext) -> None: ...
    def windows(
        self, app: DesktopApplication, limit: int, context: ToolContext
    ) -> tuple[WindowInfo, ...]: ...
    def window(self, handle: int, context: ToolContext) -> WindowInfo: ...
    def controls(
        self, window: WindowInfo, rule: ControlRule, context: ToolContext
    ) -> tuple[ControlInfo, ...]: ...
    def launch(
        self, app: DesktopApplication, context: ToolContext
    ) -> tuple[int, str]: ...
    def alive(self, pid: int, created: str, context: ToolContext) -> bool: ...
    def focused(self, window: WindowInfo, context: ToolContext) -> bool: ...
    def act(
        self,
        operation: str,
        window: WindowInfo,
        rule: ControlRule | None,
        control: ControlInfo | None,
        text: str | None,
        context: ToolContext,
    ) -> None: ...
    def verify(
        self,
        operation: str,
        window: WindowInfo,
        rule: ControlRule | None,
        text: str | None,
        context: ToolContext,
    ) -> bool: ...
