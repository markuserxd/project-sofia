"""Opt-in workspace tools; trusted controllers own roots and all approvals."""

import hashlib
import json
import logging
import os
import stat
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from sofia.security import AccountingBlocked, Action, RunGuardrails
from sofia.tools.registry import ToolContext, ToolRegistry
from sofia.tools.schemas import JSONValue, ToolDefinition, ToolParameter, valid_name
from sofia.tools.workspace_paths import (
    Directory,
    WorkspaceError,
    check_type,
    configured_root,
    identity,
    private_parts,
    relative_parts,
    walk_directory,
)


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceConfig:
    root: Path
    max_bytes: int = 8192
    max_entries: int = 64
    # This is an application trust assertion, NOT a lock or model permission.
    # The controller must exclude other writers during each mutation invocation.
    exclusive_access: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= 8192
            or type(self.max_entries) is not int
            or not 1 <= self.max_entries <= 64
            or type(self.exclusive_access) is not bool
        ):
            raise WorkspaceError("invalid_workspace")


@dataclass(frozen=True, slots=True, repr=False)
class FileVersion:
    identity: tuple[int, int]
    size: int
    modified_ns: int
    changed_ns: int
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class WorkspacePreview:
    """Immutable exact proposal for local review; its identifier is not approval."""

    preview_id: str
    workspace_id: str
    operation: str
    path: str
    parents: tuple[tuple[int, int], ...]
    expected: FileVersion | None
    content: str | None
    digest: str | None
    byte_count: int


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise WorkspaceError("invalid_encoding") from None
    if any(ord(c) < 32 and c not in "\r\n\t" for c in text) or "\x7f" in text:
        raise WorkspaceError("binary_blocked")
    # A filename denylist cannot detect renamed keys. Block common private-key
    # envelopes as an additional defense, without claiming general secret detection.
    if "PRIVATE KEY-----" in text:
        raise WorkspaceError("sensitive_file")
    return text


class WorkspaceFiles:
    """One root and one run, explicitly registered through ToolRegistry only.

    There are no public mutation methods or approval methods. Scope helpers and
    proposal inspection are for trusted controllers, never registered as tools.
    """

    def __init__(self, config: WorkspaceConfig, *, run: RunGuardrails) -> None:
        if type(config) is not WorkspaceConfig or not isinstance(run, RunGuardrails):
            raise WorkspaceError("invalid_workspace")
        run.cancellation.checkpoint()
        self._root, self._identity = configured_root(config.root)
        self._config = config
        self._run = run
        self._workspace_id = uuid4().hex
        self._pending: dict[str, WorkspacePreview] = {}
        self._lock = Lock()
        self._registered = False
        self._closed = False
        # Keep the trusted root alive so inode/file-ID reuse cannot impersonate it
        # during a long approval delay. Windows also pins the root against rename.
        with walk_directory(
            self._root, (), self._identity, run.cancellation.checkpoint
        ) as (parent, _):
            self._release_root = weakref.finalize(self, os.close, os.dup(parent.fd))

    def __enter__(self) -> "WorkspaceFiles":
        if self._closed:
            raise WorkspaceError("workspace_closed")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the root handle and pending capabilities; tools stay disabled."""
        with self._lock:
            self._closed = True
            for preview in self._pending.values():
                self._run.permissions.revoke(
                    self._action(preview.operation),
                    self._approval_scope(preview.preview_id),
                )
            self._pending.clear()
            self._release_root()

    @property
    def permission_scope(self) -> str:
        return "workspace:" + self._workspace_id

    def path_scope(self, path: str, *, directory: bool = False) -> str:
        parts = self._parts(path, directory=directory)
        canonical = "/".join(parts)
        return self.permission_scope + ":path:" + _digest(canonical.encode("utf-8"))

    def proposal(self, preview_id: str) -> WorkspacePreview:
        with self._lock:
            return self._proposal(preview_id)

    def approval_scope(self, preview_id: str) -> str:
        with self._lock:
            self._proposal(preview_id)
            return self._approval_scope(preview_id)

    def discard(self, preview_id: str) -> None:
        """Trusted controller may discard a preview without performing I/O."""
        with self._lock:
            preview = self._pending.pop(preview_id, None)
            if preview is not None:
                self._run.permissions.revoke(
                    self._action(preview.operation), self._approval_scope(preview_id)
                )

    def _approval_scope(self, preview_id: str) -> str:
        return self.permission_scope + ":preview:" + preview_id

    def _proposal(self, preview_id: str) -> WorkspacePreview:
        if type(preview_id) is not str or preview_id not in self._pending:
            raise WorkspaceError("preview_not_found")
        return self._pending[preview_id]

    @staticmethod
    def _action(operation: str) -> Action:
        return Action.FILE_DELETE if operation == "delete" else Action.FILE_WRITE

    @staticmethod
    def _parts(path: str, *, directory: bool = False) -> tuple[str, ...]:
        parts = relative_parts(path, directory=directory)
        if private_parts(parts):
            raise WorkspaceError("sensitive_file")
        return parts

    def register(self, registry: ToolRegistry, *, prefix: str = "workspace") -> None:
        if (
            registry.run_guardrails is not self._run
            or not valid_name(prefix)
            or len(prefix) > 45
        ):
            raise WorkspaceError("invalid_registration")
        path = (ToolParameter("path", "string"),)
        preview = path + (
            ToolParameter("operation", "string"),
            ToolParameter("content", "string", required=False),
        )
        specs = (
            ("list", path, Action.SENSITIVE_READ),
            ("read", path, Action.SENSITIVE_READ),
            ("preview", preview, Action.SENSITIVE_READ),
            ("create", (ToolParameter("preview_id", "string"),), Action.FILE_WRITE),
            ("replace", (ToolParameter("preview_id", "string"),), Action.FILE_WRITE),
            ("delete", (ToolParameter("preview_id", "string"),), Action.FILE_DELETE),
        )
        with self._lock:
            if self._registered or any(
                registry.contains(prefix + "_" + name) for name, _, _ in specs
            ):
                raise WorkspaceError("invalid_registration")
            for name, parameters, action in specs:

                def handler(
                    arguments: Mapping[str, JSONValue],
                    context: ToolContext,
                    operation: str = name,
                ) -> JSONValue:
                    return self._execute(operation, arguments, context)

                registry.register(
                    ToolDefinition(
                        prefix + "_" + name,
                        "Bounded workspace " + name + "; local data only",
                        parameters,
                    ),
                    handler,
                    action=action,
                    target=self.permission_scope,
                )
            self._registered = True

    def _checkpoint(self, context: ToolContext) -> None:
        context.checkpoint()
        if self._run.budget.snapshot.blocked:
            raise AccountingBlocked()

    def _authorize(
        self,
        action: Action,
        path: str,
        *,
        preview_id: str | None = None,
        directory: bool = False,
    ) -> None:
        self._run.permissions.check(action, self.permission_scope)
        self._run.permissions.check(action, self.path_scope(path, directory=directory))
        if preview_id is not None:
            self._run.permissions.check(action, self._approval_scope(preview_id))

    def _execute(
        self, operation: str, arguments: Mapping[str, JSONValue], context: ToolContext
    ) -> JSONValue:
        self._checkpoint(context)
        if not self._lock.acquire(blocking=False):
            return {"status": "error", "code": "workspace_busy"}
        try:
            if self._closed:
                raise WorkspaceError("workspace_closed")
            if operation in ("create", "replace", "delete"):
                preview_id = arguments["preview_id"]
                assert isinstance(preview_id, str)
                result = self._commit(operation, preview_id, context)
            else:
                path = arguments["path"]
                assert isinstance(path, str)
                parts = self._parts(path, directory=operation == "list")
                self._authorize(
                    Action.SENSITIVE_READ, path, directory=operation == "list"
                )
                self._checkpoint(context)
                with walk_directory(
                    self._root,
                    parts if operation == "list" else parts[:-1],
                    self._identity,
                    lambda: self._checkpoint(context),
                ) as (parent, ancestry):
                    if operation == "list":
                        result = self._list(parent, parts, context)
                    elif operation == "read":
                        content, version = self._read(parent, parts[-1], context)
                        result = {
                            "status": "ok",
                            "path": path,
                            "content": content,
                            "bytes": version.size,
                            "digest": version.digest,
                        }
                    else:
                        result = self._preview(
                            arguments, parent, ancestry, parts, context
                        )
            self._checkpoint(context)
            return result
        except WorkspaceError as error:
            return {
                "status": "error",
                "code": error.code
                if type(error) is WorkspaceError
                else "filesystem_error",
            }
        except FileNotFoundError:
            return {"status": "error", "code": "not_found"}
        except FileExistsError:
            return {"status": "error", "code": "already_exists"}
        except PermissionError:
            return {"status": "error", "code": "filesystem_denied"}
        except OSError:
            return {"status": "error", "code": "filesystem_error"}
        finally:
            self._lock.release()

    def _read(
        self, parent: Directory, name: str, context: ToolContext
    ) -> tuple[str, FileVersion]:
        self._checkpoint(context)
        fd = parent.open_read(name)
        try:
            before = os.fstat(fd)
            if before.st_size > self._config.max_bytes:
                raise WorkspaceError("size_limit")
            data = bytearray()
            while len(data) <= self._config.max_bytes:
                self._checkpoint(context)
                chunk = os.read(fd, min(4096, self._config.max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(fd)
            if len(data) > self._config.max_bytes:
                raise WorkspaceError("size_limit")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(data) != after.st_size:
                raise WorkspaceError("concurrent_modification")
            check_type(after)
            content = _text(bytes(data))
            version = FileVersion(
                identity(after),
                len(data),
                after.st_mtime_ns,
                after.st_ctime_ns,
                _digest(bytes(data)),
            )
            self._checkpoint(context)
            return content, version
        finally:
            os.close(fd)

    def _list(
        self, parent: Directory, parts: tuple[str, ...], context: ToolContext
    ) -> JSONValue:
        entries: list[tuple[str, str]] = []
        with parent.entries() as iterator:
            for index, entry in enumerate(iterator):
                self._checkpoint(context)
                # Bound examined entries too, including excluded/private names.
                # Fail instead of returning a filesystem-order-dependent subset.
                if index >= self._config.max_entries:
                    raise WorkspaceError("entry_limit")
                try:
                    child = relative_parts(entry.name)
                    if private_parts(child):
                        continue
                    info = parent.stat(entry.name)
                    directory = stat.S_ISDIR(info.st_mode)
                    check_type(info, directory=directory)
                    if info.st_dev != parent.device:
                        continue
                except (WorkspaceError, OSError):
                    continue
                entries.append(
                    (
                        "/".join((*parts, entry.name)),
                        "directory" if directory else "file",
                    )
                )
        return {
            "status": "ok",
            "entries": [{"path": path, "type": kind} for path, kind in sorted(entries)],
            "count": len(entries),
        }

    def _preview(
        self,
        arguments: Mapping[str, JSONValue],
        parent: Directory,
        ancestry: tuple[tuple[int, int], ...],
        parts: tuple[str, ...],
        context: ToolContext,
    ) -> JSONValue:
        operation = arguments["operation"]
        content = arguments.get("content")
        if (
            operation not in ("create", "replace", "delete")
            or (operation == "delete" and "content" in arguments)
            or (operation != "delete" and not isinstance(content, str))
        ):
            raise WorkspaceError("invalid_operation")
        assert isinstance(operation, str)
        if len(self._pending) >= 32:
            raise WorkspaceError("preview_limit")
        data = b""
        if isinstance(content, str):
            try:
                data = content.encode("utf-8", errors="strict")
            except UnicodeError:
                raise WorkspaceError("invalid_encoding") from None
            if len(data) > self._config.max_bytes:
                raise WorkspaceError("size_limit")
            _text(data)
        expected = None
        if operation == "create":
            try:
                parent.stat(parts[-1])
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceError("already_exists")
        else:
            _, expected = self._read(parent, parts[-1], context)
        preview = WorkspacePreview(
            uuid4().hex,
            self._workspace_id,
            operation,
            "/".join(parts),
            ancestry,
            expected,
            content if isinstance(content, str) else None,
            _digest(data) if operation != "delete" else None,
            len(data),
        )
        self._pending[preview.preview_id] = preview
        return {
            "status": "ok",
            "preview_id": preview.preview_id,
            "operation": operation,
            "path": preview.path,
            "exists": expected is not None,
            "expected_digest": expected.digest if expected else None,
            "proposed_digest": preview.digest,
            "bytes": preview.byte_count,
        }

    def _validate_version(
        self,
        preview: WorkspacePreview,
        parent: Directory,
        ancestry: tuple[tuple[int, int], ...],
        context: ToolContext,
    ) -> None:
        if ancestry != preview.parents:
            raise WorkspaceError("stale_preview")
        name = preview.path.split("/")[-1]
        if preview.expected is None:
            try:
                parent.stat(name)
            except FileNotFoundError:
                return
            raise WorkspaceError("stale_preview")
        try:
            _, current = self._read(parent, name, context)
        except (FileNotFoundError, WorkspaceError):
            raise WorkspaceError("stale_preview") from None
        if current != preview.expected:
            raise WorkspaceError("stale_preview")

    def _commit(
        self, operation: str, preview_id: str, context: ToolContext
    ) -> JSONValue:
        if not self._config.exclusive_access:
            raise WorkspaceError("exclusive_access_required")
        preview = self._proposal(preview_id)
        if preview.operation != operation:
            raise WorkspaceError("approval_mismatch")
        action = self._action(operation)
        self._authorize(action, preview.path, preview_id=preview_id)
        # Consume once after admission, including stale, cancelled and failed I/O.
        # Denial before admission leaves the proposal available for human review.
        del self._pending[preview_id]
        temporary = None
        committed = False
        cleanup_failed = False
        try:
            parts = self._parts(preview.path)
            with walk_directory(
                self._root,
                parts[:-1],
                self._identity,
                lambda: self._checkpoint(context),
            ) as (parent, ancestry):
                self._validate_version(preview, parent, ancestry, context)
                try:
                    if operation != "delete":
                        self._checkpoint(context)
                        candidate = ".sofia-" + uuid4().hex + ".tmp"
                        fd = parent.create(candidate)
                        temporary = candidate
                        try:
                            if operation == "replace":
                                parent.copy_permissions(parts[-1], temporary)
                            assert preview.content is not None
                            data = preview.content.encode("utf-8")
                            offset = 0
                            while offset < len(data):
                                self._checkpoint(context)
                                written = os.write(fd, data[offset : offset + 4096])
                                if written <= 0:
                                    raise WorkspaceError("write_failed")
                                offset += written
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    self._validate_version(preview, parent, ancestry, context)
                    self._authorize(action, preview.path, preview_id=preview_id)
                    self._checkpoint(context)
                    if operation == "delete":
                        parent.unlink(parts[-1])
                    else:
                        assert temporary is not None
                        parent.publish(
                            temporary, parts[-1], replace=operation == "replace"
                        )
                    committed = True
                finally:
                    if temporary is not None:
                        try:
                            parent.unlink(temporary)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            cleanup_failed = True
                self._checkpoint(context)
                return {
                    "status": "ok",
                    "operation": operation,
                    "committed": True,
                    "digest": preview.digest,
                    "bytes": preview.byte_count,
                    "cleanup_failed": cleanup_failed,
                }
        finally:
            self._run.permissions.revoke(action, self._approval_scope(preview_id))
            # Record the irreversible boundary even if registry's post-check
            # reports cancellation/timeout. No content, target or filename enters logs.
            try:
                logging.getLogger("sofia.workspace").info(
                    json.dumps(
                        {
                            "event": "workspace_mutation",
                            "operation": operation,
                            "committed": committed,
                            "cleanup_failed": cleanup_failed,
                            "approval_checked": True,
                        }
                    )
                )
            except Exception:
                pass
