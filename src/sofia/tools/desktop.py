"""Explicit, run-bound desktop tools. Controllers own configuration and approval."""

import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic, sleep
from uuid import uuid4

from sofia.security import (
    AccountingBlocked,
    Action,
    OperationCancelled,
    PermissionDenied,
    RunGuardrails,
)
from sofia.tools.desktop_backend import (
    ControlInfo,
    ControlRule,
    DesktopApplication,
    DesktopBackend,
    DesktopError,
    WindowInfo,
)
from sofia.tools.registry import ToolContext, ToolRegistry, ToolTimeout
from sofia.tools.schemas import JSONValue, ToolDefinition, ToolParameter, valid_name

_RESULT_CODES = frozenset(
    {
        "unknown_application",
        "unsupported_operation",
        "preview_not_found",
        "stale_preview",
        "preview_mismatch",
        "target_limit",
        "target_mismatch",
        "target_not_found",
        "ambiguous_target",
        "unsafe_target",
        "invalid_action",
        "unsafe_text",
        "preview_limit",
        "already_running",
        "launch_failed",
        "timeout",
        "focus_mismatch",
        "unsupported_session",
        "unsupported_platform",
        "desktop_dependency_missing",
        "invalid_executable",
    }
)


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    applications: tuple[DesktopApplication, ...] = ()
    max_windows: int = 16
    max_text: int = 256
    timeout: float = 5.0
    preview_ttl: float = 60.0
    max_pending: int = 32
    readiness_attempts: int = 40

    def __post_init__(self) -> None:
        if (
            type(self.applications) is not tuple
            or len(self.applications) > 16
            or any(type(a) is not DesktopApplication for a in self.applications)
            or len({a.app_id for a in self.applications}) != len(self.applications)
            or len({a.version.path for a in self.applications})
            != len(self.applications)
        ):
            raise DesktopError("invalid_config")
        for value, upper in (
            (self.max_windows, 32),
            (self.max_text, 1024),
            (self.max_pending, 64),
            (self.readiness_attempts, 100),
        ):
            if type(value) is not int or not 1 <= value <= upper:
                raise DesktopError("invalid_config")
        for duration, upper in ((self.timeout, 30), (self.preview_ttl, 300)):
            if (
                type(duration) not in (int, float)
                or not math.isfinite(duration)
                or not 0 < duration <= upper
            ):
                raise DesktopError("invalid_config")


@dataclass(frozen=True, slots=True, repr=False)
class DesktopActionPreview:
    preview_id: str
    operation: str
    app_id: str
    window_id: str | None
    window: WindowInfo | None
    control_key: str | None
    control: ControlInfo | None
    text: str | None = field(repr=False)
    text_digest: str | None
    expires: float


class DesktopAutomation:
    """No approval tool, implicit registration, workers, retries, or global input."""

    def __init__(
        self,
        config: DesktopConfig,
        *,
        run: RunGuardrails,
        backend: DesktopBackend | None = None,
        clock: Callable[[], float] = monotonic,
        pause: Callable[[float], None] = sleep,
    ) -> None:
        if type(config) is not DesktopConfig or not isinstance(run, RunGuardrails):
            raise DesktopError("invalid_config")
        self._config, self._run = config, run
        self._backend = backend
        self._clock, self._pause = clock, pause
        self._apps = {app.app_id: app for app in config.applications}
        self._id = uuid4().hex
        self._windows: dict[str, tuple[str, WindowInfo]] = {}
        self._pending: dict[str, DesktopActionPreview] = {}
        self._lock = Lock()
        self._registered = False

    @property
    def permission_scope(self) -> str:
        return "desktop:" + self._id

    def target_scope(
        self,
        app_id: str,
        operation: str,
        *,
        window_id: str | None = None,
        control: str | None = None,
    ) -> str:
        """Exact controller scope; never use a title or model-provided permission."""
        self._app(app_id)
        if operation not in ("observe", "launch", "focus", "click", "text"):
            raise DesktopError("unsupported_operation")
        return (
            self.permission_scope
            + ":target:"
            + hashlib.sha256(
                json.dumps([app_id, operation, window_id, control]).encode()
            ).hexdigest()
        )

    def proposal(self, preview_id: str) -> DesktopActionPreview:
        """Private local review includes proposed text; never send it to audit logs."""
        with self._lock:
            return self._proposal(preview_id)

    def approval_scope(self, preview_id: str) -> str:
        with self._lock:
            self._proposal(preview_id)
            return self._approval_scope(preview_id)

    def _approval_scope(self, preview_id: str) -> str:
        return self.permission_scope + ":preview:" + preview_id

    def discard(self, preview_id: str) -> None:
        with self._lock:
            self._discard(preview_id)

    def _discard(self, preview_id: str) -> None:
        preview = self._pending.pop(preview_id, None)
        if preview:
            self._run.permissions.revoke(
                self._action(preview.operation), self._approval_scope(preview_id)
            )

    def _proposal(self, preview_id: str) -> DesktopActionPreview:
        self._run.cancellation.checkpoint()
        if type(preview_id) is not str or preview_id not in self._pending:
            raise DesktopError("preview_not_found")
        preview = self._pending[preview_id]
        if self._clock() >= preview.expires:
            self._discard(preview_id)
            raise DesktopError("stale_preview")
        return preview

    def _app(self, app_id: str) -> DesktopApplication:
        if type(app_id) is not str or app_id not in self._apps:
            raise DesktopError("unknown_application")
        return self._apps[app_id]

    @staticmethod
    def _action(operation: str) -> Action:
        return Action.APP_LAUNCH if operation == "launch" else Action.UI_INTERACT

    def register(self, registry: ToolRegistry, *, prefix: str = "desktop") -> None:
        if (
            registry.run_guardrails is not self._run
            or not valid_name(prefix)
            or len(prefix) > 45
        ):
            raise DesktopError("invalid_registration")
        app = (ToolParameter("app_id", "string"),)
        specs = (
            ("discover", app, Action.DESKTOP_OBSERVE),
            (
                "inspect",
                app + (ToolParameter("window_id", "string"),),
                Action.DESKTOP_OBSERVE,
            ),
            (
                "preview",
                app
                + (
                    ToolParameter("operation", "string"),
                    ToolParameter("window_id", "string", False),
                    ToolParameter("control", "string", False),
                    ToolParameter("text", "string", False),
                ),
                Action.DESKTOP_OBSERVE,
            ),
            ("launch", (ToolParameter("preview_id", "string"),), Action.APP_LAUNCH),
            ("interact", (ToolParameter("preview_id", "string"),), Action.UI_INTERACT),
        )
        with self._lock:
            if self._registered or any(
                registry.contains(prefix + "_" + n) for n, _, _ in specs
            ):
                raise DesktopError("invalid_registration")
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
                        "Approved bounded desktop " + name,
                        parameters,
                        self._config.timeout,
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

    def _native(self, context: ToolContext) -> DesktopBackend:
        self._checkpoint(context)
        if self._backend is None:
            from sofia.tools.windows_desktop import WindowsDesktopBackend

            self._backend = WindowsDesktopBackend()
        self._backend.session(context)
        self._checkpoint(context)
        return self._backend

    def _authorize(
        self,
        action: Action,
        app_id: str,
        operation: str,
        window_id: str | None = None,
        control: str | None = None,
    ) -> None:
        self._run.permissions.check(action, self.permission_scope)
        self._run.permissions.check(
            action,
            self.target_scope(app_id, operation, window_id=window_id, control=control),
        )

    def _remember(self, app: DesktopApplication, window: WindowInfo) -> str:
        for key, (app_id, existing) in self._windows.items():
            if app_id == app.app_id and existing.identity == window.identity:
                return key
        if len(self._windows) >= 64:
            raise DesktopError("target_limit")
        key = uuid4().hex
        self._windows[key] = app.app_id, window
        return key

    def _validate_window(self, app: DesktopApplication, window: WindowInfo) -> None:
        if (
            window.executable != app.version.path
            or window.window_class != app.window_class
            or type(window.handle) is not int
            or window.handle <= 0
            or type(window.pid) is not int
            or window.pid <= 0
            or not window.created
        ):
            raise DesktopError("target_mismatch")

    def _window(
        self,
        app: DesktopApplication,
        window_id: str,
        backend: DesktopBackend,
        context: ToolContext,
        *,
        actionable: bool = False,
    ) -> WindowInfo:
        if window_id not in self._windows or self._windows[window_id][0] != app.app_id:
            raise DesktopError("target_not_found")
        old = self._windows[window_id][1]
        self._checkpoint(context)
        current = backend.window(old.handle, context)
        self._validate_window(app, current)
        if current.identity != old.identity:
            raise DesktopError("target_mismatch")
        if actionable and (
            not current.visible or not current.enabled or current.minimized
        ):
            raise DesktopError("unsafe_target")
        return current

    @staticmethod
    def _rule(app: DesktopApplication, key: str, operation: str) -> ControlRule:
        matches = [c for c in app.controls if c.key == key and c.operation == operation]
        if len(matches) != 1:
            raise DesktopError("unsafe_target")
        return matches[0]

    def _control(
        self,
        backend: DesktopBackend,
        window: WindowInfo,
        rule: ControlRule,
        context: ToolContext,
    ) -> ControlInfo:
        self._checkpoint(context)
        controls = backend.controls(window, rule, context)
        if not controls:
            raise DesktopError("target_not_found")
        if len(controls) != 1:
            raise DesktopError("ambiguous_target")
        control = controls[0]
        if (
            not control.runtime_id
            or control.automation_id != rule.automation_id
            or control.control_type != rule.control_type
            or control.password
            or not control.visible
            or not control.enabled
            or (rule.operation == "text" and not control.editable)
        ):
            raise DesktopError("unsafe_target")
        return control

    def _execute(
        self, operation: str, args: Mapping[str, JSONValue], context: ToolContext
    ) -> JSONValue:
        if not self._lock.acquire(blocking=False):
            return {"status": "error", "code": "desktop_busy"}
        try:
            self._checkpoint(context)
            if operation in ("launch", "interact"):
                preview_id = args["preview_id"]
                assert isinstance(preview_id, str)
                preview = self._proposal(preview_id)
                if (operation == "launch") != (preview.operation == "launch"):
                    raise DesktopError("preview_mismatch")
                return self._commit(preview, context)
            app_id = args["app_id"]
            assert isinstance(app_id, str)
            app = self._app(app_id)
            self._authorize(Action.DESKTOP_OBSERVE, app_id, "observe")
            backend = self._native(context)
            app.revalidate()
            if operation == "discover":
                windows = backend.windows(app, self._config.max_windows + 1, context)
                if len(windows) > self._config.max_windows:
                    raise DesktopError("target_limit")
                if len({w.handle for w in windows}) != len(windows):
                    raise DesktopError("ambiguous_target")
                result: list[JSONValue] = []
                for window in windows:
                    self._checkpoint(context)
                    self._validate_window(app, window)
                    result.append(
                        {
                            "window_id": self._remember(app, window),
                            "visible": window.visible,
                            "enabled": window.enabled,
                            "minimized": window.minimized,
                        }
                    )
                self._checkpoint(context)
                self._audit("discovery", "ok")
                return {"status": "ok", "windows": result}
            if operation == "preview":
                return self._preview(app, args, backend, context)
            window_id = args["window_id"]
            assert isinstance(window_id, str)
            window = self._window(app, window_id, backend, context)
            result = []
            for rule in app.controls:
                control = self._control(backend, window, rule, context)
                result.append(
                    {
                        "control": rule.key,
                        "type": rule.control_type,
                        "enabled": control.enabled,
                        "visible": control.visible,
                    }
                )
            self._checkpoint(context)
            return {
                "status": "ok",
                "controls": result,
                "active": backend.focused(window, context),
            }
        except PermissionDenied:
            self._audit("approval_denied", "permission_denied")
            raise
        except OperationCancelled:
            self._audit("cancellation", "cancelled")
            raise
        except ToolTimeout:
            self._audit("timeout", "timeout")
            raise
        except DesktopError as error:
            code = (
                error.args[0]
                if type(error) is DesktopError and error.args
                else "desktop_failed"
            )
            if type(code) is not str or code not in _RESULT_CODES:
                code = "desktop_failed"
            self._checkpoint(context)
            self._audit("rejected", code)
            return {"status": "error", "code": code}
        except Exception:
            self._checkpoint(context)
            self._audit("rejected", "desktop_failed")
            return {"status": "error", "code": "desktop_failed"}
        finally:
            self._lock.release()

    def _preview(
        self,
        app: DesktopApplication,
        args: Mapping[str, JSONValue],
        backend: DesktopBackend,
        context: ToolContext,
    ) -> JSONValue:
        operation = args["operation"]
        if operation not in ("launch", "focus", "click", "text"):
            raise DesktopError("unsupported_operation")
        assert isinstance(operation, str)
        window_id, key, text = (
            args.get("window_id"),
            args.get("control"),
            args.get("text"),
        )
        if (
            operation == "launch"
            and (window_id is not None or key is not None or text is not None)
            or operation != "launch"
            and type(window_id) is not str
            or operation in ("click", "text")
            and type(key) is not str
            or operation == "focus"
            and key is not None
            or operation != "text"
            and text is not None
        ):
            raise DesktopError("invalid_action")
        if operation == "text" and (
            type(text) is not str
            or not 1 <= len(text) <= self._config.max_text
            or any(not c.isprintable() for c in text)
        ):
            raise DesktopError("unsafe_text")
        assert window_id is None or isinstance(window_id, str)
        assert key is None or isinstance(key, str)
        assert text is None or isinstance(text, str)
        window = None
        control = None
        if window_id is not None:
            window = self._window(app, window_id, backend, context, actionable=True)
        if key is not None:
            assert window is not None
            control = self._control(
                backend, window, self._rule(app, key, operation), context
            )
        for expired in tuple(self._pending):
            if self._clock() >= self._pending[expired].expires:
                self._discard(expired)
        if len(self._pending) >= self._config.max_pending:
            raise DesktopError("preview_limit")
        self._checkpoint(context)
        preview = DesktopActionPreview(
            uuid4().hex,
            operation,
            app.app_id,
            window_id,
            window,
            key,
            control,
            text,
            hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
            self._clock() + self._config.preview_ttl,
        )
        self._pending[preview.preview_id] = preview
        self._audit("preview_created", "ok")
        return {
            "status": "ok",
            "preview_id": preview.preview_id,
            "operation": operation,
            "app_id": app.app_id,
            "window_id": window_id,
            "control": key,
            "text_length": len(text) if text is not None else 0,
        }

    def _commit(self, preview: DesktopActionPreview, context: ToolContext) -> JSONValue:
        self._checkpoint(context)
        action = self._action(preview.operation)
        self._authorize(
            action,
            preview.app_id,
            preview.operation,
            preview.window_id,
            preview.control_key,
        )
        self._run.permissions.check(action, self._approval_scope(preview.preview_id))
        # Burn before native work, including failed/ambiguous attempts; no implicit retry.
        self._discard(preview.preview_id)
        app = self._app(preview.app_id)
        app.revalidate()
        backend = self._native(context)
        self._audit("action_started", "ok")
        if preview.operation == "launch":
            if not app.multiple_instances and backend.windows(app, 1, context):
                raise DesktopError("already_running")
            self._checkpoint(context)
            pid, created = backend.launch(app, context)
            for _ in range(self._config.readiness_attempts):
                self._checkpoint(context)
                backend.session(context)
                if not backend.alive(pid, created, context):
                    raise DesktopError("launch_failed")
                windows = backend.windows(app, self._config.max_windows + 1, context)
                if len(windows) > self._config.max_windows:
                    raise DesktopError("target_limit")
                matches = [w for w in windows if w.pid == pid and w.created == created]
                if len(matches) > 1:
                    raise DesktopError("ambiguous_target")
                if matches:
                    window = matches[0]
                    self._validate_window(app, window)
                    if window.visible and window.enabled and not window.minimized:
                        self._checkpoint(context)
                        key = self._remember(app, window)
                        self._audit("action_completed", "ok")
                        return {"status": "ok", "verified": True, "window_id": key}
                self._pause(0.05)
            raise DesktopError("timeout")
        assert preview.window_id is not None and preview.window is not None
        window = self._window(app, preview.window_id, backend, context, actionable=True)
        if window.identity != preview.window.identity:
            raise DesktopError("target_mismatch")
        rule, control = None, None
        if preview.control_key:
            rule = self._rule(app, preview.control_key, preview.operation)
            control = self._control(backend, window, rule, context)
            if control != preview.control:
                raise DesktopError("target_mismatch")
        if preview.operation != "focus" and not backend.focused(window, context):
            raise DesktopError("focus_mismatch")
        backend.session(context)
        self._checkpoint(context)
        window = self._window(app, preview.window_id, backend, context, actionable=True)
        self._checkpoint(context)
        backend.act(preview.operation, window, rule, control, preview.text, context)
        self._checkpoint(context)
        self._window(app, preview.window_id, backend, context, actionable=True)
        verified = backend.verify(
            preview.operation, window, rule, preview.text, context
        )
        self._checkpoint(context)
        self._audit("action_completed", "ok" if verified else "verification_failed")
        return {
            "status": "ok" if verified else "error",
            "verified": verified,
            "code": "ok" if verified else "verification_failed",
        }

    @staticmethod
    def _audit(event: str, code: str) -> None:
        try:
            logging.getLogger("sofia.desktop").info(
                json.dumps({"event": "desktop_" + event, "code": code})
            )
        except Exception:
            pass
