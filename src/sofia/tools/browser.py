"""Run-bound opt-in browser tools. Only registry handlers execute browser actions."""

import hashlib
import json
import logging
import os
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import uuid4

from sofia.security import (
    AccountingBlocked,
    Action,
    GuardrailError,
    PermissionDenied,
    RunGuardrails,
)
from sofia.tools.browser_backend import BrowserBackend, PlaywrightBrowserBackend
from sofia.tools.browser_policy import BrowserConfig, BrowserError, BrowserOriginPolicy
from sofia.tools.registry import ToolContext, ToolRegistry, ToolTimeout
from sofia.tools.schemas import JSONValue, ToolDefinition, ToolParameter, valid_name
from sofia.tools.workspace_paths import configured_root, walk_directory


@dataclass(frozen=True, slots=True, repr=False)
class BrowserActionPreview:
    preview_id: str
    session_id: str
    page_id: str
    origin: str
    operation: str
    target: str
    version: int
    state: str = field(repr=False)
    digest: str
    destination: str = field(repr=False)
    method: str
    expires: float


@dataclass(slots=True, repr=False)
class _Session:
    policy: BrowserOriginPolicy
    page_id: str
    expires: float
    operations: int = 0
    version: int = 0
    loaded: bool = False
    downloads: int = 0


class BrowserAutomation:
    """Trusted code owns manifests, approval decisions and exclusive quarantine access.

    The quarantine is a separate controller-owned local directory, not a bypass
    into WorkspaceFiles roots. No model-controlled filesystem paths are accepted.
    """

    def __init__(
        self,
        config: BrowserConfig,
        *,
        run: RunGuardrails,
        backend: BrowserBackend | None = None,
        quarantine: Path | None = None,
        exclusive_access: bool = False,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            type(config) is not BrowserConfig
            or not isinstance(run, RunGuardrails)
            or type(exclusive_access) is not bool
        ):
            raise BrowserError("invalid_config")
        run.cancellation.checkpoint()
        self._config, self._run, self._clock = config, run, clock
        self._backend = (
            backend if backend is not None else PlaywrightBrowserBackend(config.timeout)
        )
        self._id = uuid4().hex
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, BrowserActionPreview] = {}
        self._lock = Lock()
        self._closed = self._registered = False
        self._quarantine = None
        self._root_identity = None
        self._release_root: Callable[[], None] = lambda: None
        if quarantine is not None:
            if not exclusive_access:
                raise BrowserError("exclusive_access_required")
            self._quarantine, self._root_identity = configured_root(quarantine)
            with walk_directory(
                self._quarantine, (), self._root_identity, run.cancellation.checkpoint
            ) as (parent, _):
                self._release_root = weakref.finalize(self, os.close, os.dup(parent.fd))

    def __enter__(self) -> "BrowserAutomation":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Unconditional trusted cleanup, including after cancellation or exhausted budgets."""
        with self._lock:
            self._closed = True
            for preview_id in tuple(self._pending):
                self._discard(preview_id)
            self._sessions.clear()
            try:
                self._backend.close_all()
            finally:
                self._release_root()

    @property
    def permission_scope(self) -> str:
        return "browser:" + self._id

    def target_scope(
        self, origin_id: str, operation: str, *, session_id: str = "", target: str = ""
    ) -> str:
        policy = self._policy(origin_id)
        if operation not in {
            "start",
            "navigate",
            "observe",
            "fill",
            "click",
            "preview_submit",
            "submit",
            "preview_download",
            "download",
            "close",
        }:
            raise BrowserError("unsupported_operation")
        return (
            self.permission_scope
            + ":origin:"
            + policy.origin
            + ":"
            + hashlib.sha256(
                json.dumps([operation, session_id, target]).encode()
            ).hexdigest()
        )

    def _policy(self, key: str) -> BrowserOriginPolicy:
        for policy in self._config.origins:
            if policy.origin_id == key:
                return policy
        raise BrowserError("origin_denied")

    def _scope(self, preview_id: str) -> str:
        return self.permission_scope + ":preview:" + preview_id

    def proposal(self, preview_id: str) -> BrowserActionPreview:
        with self._lock:
            return self._proposal(preview_id)

    def approval_scope(self, preview_id: str) -> str:
        with self._lock:
            self._proposal(preview_id)
            return self._scope(preview_id)

    def discard(self, preview_id: str) -> None:
        with self._lock:
            self._discard(preview_id)

    def _discard(self, preview_id: str) -> None:
        preview = self._pending.pop(preview_id, None)
        if preview:
            self._run.permissions.revoke(
                self._action(preview.operation), self._scope(preview_id)
            )

    def _proposal(self, preview_id: str) -> BrowserActionPreview:
        self._run.cancellation.checkpoint()
        preview = self._pending.get(preview_id)
        if preview is None:
            raise BrowserError("preview_not_found")
        if self._clock() >= preview.expires:
            self._discard(preview_id)
            raise BrowserError("stale_preview")
        return preview

    @staticmethod
    def _action(operation: str) -> Action:
        if operation == "submit":
            return Action.FORM_SUBMIT
        if operation == "download":
            return Action.FILE_WRITE
        if operation == "start":
            return Action.APP_LAUNCH
        if operation.startswith("preview_") or operation == "observe":
            return Action.SENSITIVE_READ
        return Action.UI_INTERACT

    def register(self, registry: ToolRegistry, *, prefix: str = "browser") -> None:
        if (
            registry.run_guardrails is not self._run
            or not valid_name(prefix)
            or len(prefix) > 40
        ):
            raise BrowserError("invalid_registration")
        session = (ToolParameter("session_id", "string"),)
        target = session + (ToolParameter("target", "string"),)
        preview = (ToolParameter("preview_id", "string"),)
        specs = (
            ("start", (ToolParameter("origin_id", "string"),)),
            ("navigate", session + (ToolParameter("url", "string"),)),
            ("observe", target),
            ("click", target),
            ("fill", target + (ToolParameter("text", "string"),)),
            ("preview_submit", target),
            ("submit", preview),
            ("preview_download", target),
            ("download", preview),
            ("close", session),
        )
        with self._lock:
            if (
                self._registered
                or self._closed
                or any(registry.contains(prefix + "_" + n) for n, _ in specs)
            ):
                raise BrowserError("invalid_registration")
            for name, parameters in specs:

                def handler(
                    args: Mapping[str, JSONValue],
                    context: ToolContext,
                    operation: str = name,
                ) -> JSONValue:
                    return self._execute(operation, args, context)

                registry.register(
                    ToolDefinition(
                        prefix + "_" + name,
                        "Controlled local fixture browser " + name,
                        parameters,
                        self._config.timeout,
                    ),
                    handler,
                    action=self._action(name),
                    target=self.permission_scope,
                )
            self._registered = True

    def _checkpoint(self, context: ToolContext) -> None:
        context.checkpoint()
        if self._run.budget.snapshot.blocked:
            raise AccountingBlocked()

    def _authorize(
        self,
        policy: BrowserOriginPolicy,
        operation: str,
        session: str = "",
        target: str = "",
    ) -> None:
        action = self._action(operation)
        self._run.permissions.check(action, self.permission_scope)
        self._run.permissions.check(
            action,
            self.target_scope(
                policy.origin_id, operation, session_id=session, target=target
            ),
        )

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise BrowserError("unknown_session")
        if (
            self._clock() >= session.expires
            or session.operations >= self._config.max_operations
        ):
            self._end_session(session_id)
            raise BrowserError("session_limit")
        session.operations += 1
        return session

    def _end_session(self, session_id: str) -> None:
        for key, preview in tuple(self._pending.items()):
            if preview.session_id == session_id:
                self._discard(key)
        self._sessions.pop(session_id, None)
        self._backend.close(session_id)

    def _changed(self, session_id: str, session: _Session) -> None:
        session.version += 1
        for key, preview in tuple(self._pending.items()):
            if preview.session_id == session_id:
                self._discard(key)

    def _execute(
        self, operation: str, args: Mapping[str, JSONValue], context: ToolContext
    ) -> JSONValue:
        if not self._lock.acquire(blocking=False):
            return {"status": "error", "code": "browser_busy"}
        session_id = ""
        approval_checked = False
        cleanup_failed = False
        status = "error"
        try:
            self._checkpoint(context)
            if self._closed:
                raise BrowserError("browser_closed")
            if operation == "start":
                origin_id = args["origin_id"]
                assert isinstance(origin_id, str)
                policy = self._policy(origin_id)
                self._authorize(policy, operation)
                # Expired sessions cannot occupy capacity indefinitely.
                for key, old in tuple(self._sessions.items()):
                    if self._clock() >= old.expires:
                        self._end_session(key)
                if len(self._sessions) >= self._config.max_sessions:
                    raise BrowserError("session_limit")
                session_id = uuid4().hex
                try:
                    self._backend.start(session_id, context)
                    self._checkpoint(context)
                except BaseException:
                    try:
                        self._backend.close(session_id)
                    except Exception:
                        cleanup_failed = True
                    raise
                session = _Session(
                    policy, uuid4().hex, self._clock() + self._config.session_ttl
                )
                self._sessions[session_id] = session
                status = "ok"
                return {
                    "status": status,
                    "session_id": session_id,
                    "page_id": session.page_id,
                    "origin_id": origin_id,
                }

            preview = None
            if operation in ("submit", "download"):
                pid = args["preview_id"]
                assert isinstance(pid, str)
                preview = self._proposal(pid)
                if preview.operation != operation:
                    raise BrowserError("preview_mismatch")
                session_id, target = preview.session_id, preview.target
            else:
                sid = args["session_id"]
                assert isinstance(sid, str)
                session_id = sid
                target_value = args.get("target", "")
                assert isinstance(target_value, str)
                target = target_value
            session = self._session(session_id)
            policy = session.policy
            if operation == "navigate":
                url = args["url"]
                assert isinstance(url, str)
                target = policy.path(url)
                if not any(x.path == target for x in policy.documents):
                    raise BrowserError("unknown_document")
            self._authorize(policy, operation, session_id, target)
            self._checkpoint(context)
            if operation == "close":
                self._end_session(session_id)
            elif operation == "navigate":
                self._changed(session_id, session)
                session.loaded = False
                self._backend.navigate(session_id, policy, target, context)
                session.loaded = True
            elif not session.loaded:
                raise BrowserError("page_not_loaded")
            elif operation in ("observe", "fill", "click"):
                rule = next(
                    (
                        x
                        for x in policy.elements
                        if x.key == target and x.operation == operation
                    ),
                    None,
                )
                if rule is None:
                    raise BrowserError("unsafe_target")
                if operation == "observe":
                    text = self._backend.observe(session_id, rule, context)
                    if type(text) is not str or len(text) > 256:
                        raise BrowserError("observation_limit")
                    self._checkpoint(context)
                    status = "ok"
                    return {"status": status, "text": text, "untrusted": True}
                self._changed(session_id, session)
                if operation == "fill":
                    proposed_text = args["text"]
                    if (
                        type(proposed_text) is not str
                        or not 1 <= len(proposed_text) <= self._config.max_text
                        or any(not c.isprintable() for c in proposed_text)
                    ):
                        raise BrowserError("unsafe_text")
                    if not self._backend.fill(session_id, rule, proposed_text, context):
                        raise BrowserError("interaction_unverified")
                else:
                    self._backend.click(session_id, rule, context)
                    label = next(x for x in policy.elements if x.key == rule.status_key)
                    if (
                        self._backend.observe(session_id, label, context)
                        != rule.expected_text
                    ):
                        raise BrowserError("interaction_unverified")
            elif operation.startswith("preview_"):
                if len(self._pending) >= self._config.max_pending:
                    raise BrowserError("preview_limit")
                kind = operation.removeprefix("preview_")
                state, destination, method = self._state(
                    kind, session_id, session, target, context
                )
                preview = BrowserActionPreview(
                    uuid4().hex,
                    session_id,
                    session.page_id,
                    policy.origin,
                    kind,
                    target,
                    session.version,
                    state,
                    hashlib.sha256(state.encode()).hexdigest(),
                    destination,
                    method,
                    min(session.expires, self._clock() + self._config.preview_ttl),
                )
                self._pending[preview.preview_id] = preview
                status = "ok"
                return {
                    "status": status,
                    "preview_id": preview.preview_id,
                    "operation": kind,
                }
            elif preview is not None:
                action = self._action(operation)
                self._run.permissions.check(action, self._scope(preview.preview_id))
                approval_checked = True
                # Admission consumes the exact capability even on ambiguous native failure.
                self._discard(preview.preview_id)
                state, destination, method = self._state(
                    operation, session_id, session, target, context
                )
                if (
                    session.version != preview.version
                    or session.page_id != preview.page_id
                    or policy.origin != preview.origin
                    or state != preview.state
                    or destination != preview.destination
                    or method != preview.method
                ):
                    raise BrowserError("stale_preview")
                self._checkpoint(context)
                self._changed(session_id, session)
                if operation == "submit":
                    form = next(x for x in policy.forms if x.key == target)
                    self._backend.submit(session_id, policy, form, state, context)
                else:
                    if session.downloads >= self._config.max_downloads:
                        raise BrowserError("download_limit")
                    session.downloads += 1
                    rule_download = next(x for x in policy.downloads if x.key == target)
                    body = self._backend.download(
                        session_id, policy, rule_download, context
                    )
                    if body != rule_download.body:
                        raise BrowserError("download_unverified")
                    self._save(preview.destination, body, context)
            self._checkpoint(context)
            status = "ok"
            return {"status": status, "verified": True, "page_id": session.page_id}
        except PermissionDenied:
            raise
        except (GuardrailError, ToolTimeout):
            # No more page script execution after a stopped/uncertain operation.
            if session_id:
                try:
                    self._end_session(session_id)
                except Exception:
                    cleanup_failed = True
            raise
        except Exception as error:
            # Browser/HTTP/OS exception text and DOM data never cross this boundary.
            if session_id:
                try:
                    self._end_session(session_id)
                except Exception:
                    cleanup_failed = True
            return {
                "status": "error",
                "code": "cleanup_failed"
                if cleanup_failed
                else error.code
                if type(error) is BrowserError
                else "browser_failed",
            }
        finally:
            try:
                logging.getLogger("sofia.browser").info(
                    json.dumps(
                        {
                            "event": "browser_operation",
                            "operation": operation,
                            "status": status,
                            "approval_checked": approval_checked,
                            "cleanup_failed": cleanup_failed,
                        }
                    )
                )
            except Exception:
                pass
            self._lock.release()

    def _state(
        self,
        operation: str,
        session_id: str,
        session: _Session,
        target: str,
        context: ToolContext,
    ) -> tuple[str, str, str]:
        if operation == "submit":
            form = next((x for x in session.policy.forms if x.key == target), None)
            if form is None:
                raise BrowserError("unsafe_form")
            state = self._backend.form_state(session_id, session.policy, form, context)
            if type(state) is not str or len(state) > 24576:
                raise BrowserError("unsafe_form")
            return state, session.policy.origin + form.destination, "POST"
        download = next((x for x in session.policy.downloads if x.key == target), None)
        if download is None or self._quarantine is None:
            raise BrowserError("download_disabled")
        # A random name is bound into the immutable preview; the model never supplies it.
        state = (
            hashlib.sha256(download.body).hexdigest()
            + "\n"
            + self._backend.download_state(
                session_id, session.policy, download, context
            )
        )
        # Stable destination for repeated state validation of the same session/version.
        name = "fixture-" + session_id + "-" + str(session.version) + ".txt"
        return state, name, "GET"

    def _save(self, name: str, body: bytes, context: ToolContext) -> None:
        assert self._quarantine is not None
        temporary = ".sofia-" + uuid4().hex + ".tmp"
        with walk_directory(
            self._quarantine, (), self._root_identity, lambda: self._checkpoint(context)
        ) as (parent, _):
            self._checkpoint(context)
            fd = parent.create(temporary)
            try:
                try:
                    if os.write(fd, body) != len(body):
                        raise BrowserError("write_failed")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._checkpoint(context)
                parent.publish(temporary, name, replace=False)
            finally:
                try:
                    parent.unlink(temporary)
                except FileNotFoundError:
                    pass
