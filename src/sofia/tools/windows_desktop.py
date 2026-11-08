"""Lazy Windows UIA backend. No coordinate, clipboard, shell, or keyboard fallback."""

import ctypes
import importlib
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from sofia.tools.desktop_backend import (
    ControlInfo,
    ControlRule,
    DesktopApplication,
    DesktopError,
    WindowInfo,
)
from sofia.tools.registry import ToolContext


@contextmanager
def _handle(handle: Any) -> Iterator[Any]:
    try:
        yield handle
    finally:
        handle.Close()


class WindowsDesktopBackend:
    """Single-threaded native calls; UIA providers can exceed cooperative deadlines."""

    # Optional, untyped native modules are loaded only after the platform gate.
    # Explicit annotations also let Linux type checking inspect the remaining methods.
    api: Any
    gui: Any
    process: Any
    security: Any
    ts: Any
    station: Any
    files: Any
    uia: Any
    uia_wrapper: Any
    uia_defs: Any
    uia_info: Any
    _kernel: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DesktopError("unsupported_platform")
        try:
            self.api = importlib.import_module("win32api")
            self.gui = importlib.import_module("win32gui")
            self.process = importlib.import_module("win32process")
            self.security = importlib.import_module("win32security")
            self.ts = importlib.import_module("win32ts")
            self.station = importlib.import_module("win32service")
            self.files = importlib.import_module("win32file")
            self.uia = importlib.import_module("pywinauto")
            self.uia_wrapper = importlib.import_module("pywinauto.controls.uiawrapper")
            self.uia_defs = importlib.import_module("pywinauto.uia_defines")
            self.uia_info = importlib.import_module("pywinauto.uia_element_info")
        except ImportError:
            raise DesktopError("desktop_dependency_missing") from None
        self._owned: dict[int, tuple[str, subprocess.Popen[bytes]]] = {}
        self._elements: dict[int, Any] = {}
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._kernel.QueryFullProcessImageNameW.restype = ctypes.c_int

    def _image_path(self, process: Any) -> Path:
        size = ctypes.c_uint(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self._kernel.QueryFullProcessImageNameW(
            int(process), 0, buffer, ctypes.byref(size)
        ):
            raise DesktopError("target_not_found")
        return Path(buffer.value).resolve(strict=True)

    def _token(self, process: Any) -> tuple[str, int]:
        with _handle(self.security.OpenProcessToken(process, 0x0008)) as token:
            user = self.security.GetTokenInformation(token, self.security.TokenUser)[0]
            integrity = self.security.GetTokenInformation(
                token, self.security.TokenIntegrityLevel
            )
            sid = integrity[0] if isinstance(integrity, tuple) else integrity
            level = sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
            return self.security.ConvertSidToStringSid(user), level

    def session(self, context: ToolContext) -> None:
        context.checkpoint()
        try:
            session = self.ts.ProcessIdToSessionId(os.getpid())
            if (
                session == 0
                or self.ts.WTSQuerySessionInformation(
                    None, session, self.ts.WTSConnectState
                )
                != self.ts.WTSActive
            ):
                raise DesktopError("unsupported_session")
            if (
                self.station.GetUserObjectInformation(
                    self.station.GetProcessWindowStation(), 2
                )
                != "WinSta0"
            ):
                raise DesktopError("unsupported_session")
            desktop = self.station.OpenInputDesktop(0, False, 0x0101)
            try:
                if (
                    self.station.GetUserObjectInformation(desktop, 2).lower()
                    != "default"
                ):
                    raise DesktopError("unsupported_session")
                # Inspect only; never call SwitchDesktop or move the user's session.
                thread_desktop = self.station.GetThreadDesktop(
                    self.api.GetCurrentThreadId()
                )
                if (
                    self.station.GetUserObjectInformation(thread_desktop, 2).lower()
                    != "default"
                ):
                    raise DesktopError("unsupported_session")
            finally:
                desktop.CloseDesktop()
            _, integrity = self._token(self.api.GetCurrentProcess())
            if integrity != 0x2000:
                raise DesktopError("unsupported_session")
        except DesktopError:
            raise
        except Exception:
            raise DesktopError("unsupported_session") from None
        context.checkpoint()

    def _identity(self, pid: int) -> tuple[Path, str]:
        try:
            with _handle(self.api.OpenProcess(0x1000, False, pid)) as process:
                own_user, own_level = self._token(self.api.GetCurrentProcess())
                user, level = self._token(process)
                if (
                    user != own_user
                    or level != own_level
                    or level != 0x2000
                    or self.ts.ProcessIdToSessionId(pid)
                    != self.ts.ProcessIdToSessionId(os.getpid())
                ):
                    raise DesktopError("unsafe_target")
                executable = self._image_path(process)
                created = self.process.GetProcessTimes(process)[
                    "CreationTime"
                ].isoformat()
                if executable.stem.lower().startswith("python"):
                    owned = self._owned.get(pid)
                    if (
                        not owned
                        or (owned[0] and owned[0] != created)
                        or owned[1].poll() is not None
                    ):
                        raise DesktopError("unsafe_target")
                return executable, created
        except DesktopError:
            raise
        except Exception:
            raise DesktopError("target_not_found") from None

    def window(self, handle: int, context: ToolContext) -> WindowInfo:
        self.session(context)
        if not self.gui.IsWindow(handle) or self.gui.GetAncestor(handle, 2) != handle:
            raise DesktopError("target_not_found")
        thread, pid = self.process.GetWindowThreadProcessId(handle)
        if (
            self.station.GetUserObjectInformation(
                self.station.GetThreadDesktop(thread), 2
            ).lower()
            != "default"
        ):
            raise DesktopError("unsupported_session")
        executable, created = self._identity(pid)
        # Retain the old provider object too: a closed/recreated HWND must not
        # silently replace the provider to which a pending preview was bound.
        element = (
            self.uia.Desktop(backend="uia")
            .window(handle=handle)
            .wrapper_object()
            .element_info.element
        )
        try:
            previous = self._elements.get(handle)
            runtime_id = tuple(element.GetRuntimeId())
            if previous is not None and (
                tuple(previous.GetRuntimeId()) != runtime_id
                or previous.CurrentProcessId != pid
                or previous.CurrentNativeWindowHandle != handle
            ):
                raise DesktopError("target_mismatch")
            if not runtime_id:
                raise DesktopError("target_mismatch")
        except Exception:
            raise DesktopError("target_mismatch") from None
        if handle not in self._elements:
            if len(self._elements) >= 64:
                raise DesktopError("target_limit")
            self._elements[handle] = element
        result = WindowInfo(
            handle,
            pid,
            created,
            executable,
            self.gui.GetClassName(handle),
            bool(self.gui.IsWindowVisible(handle)),
            bool(self.gui.IsWindowEnabled(handle)),
            bool(self.gui.IsIconic(handle)),
            runtime_id,
        )
        context.checkpoint()
        return result

    def windows(
        self, app: DesktopApplication, limit: int, context: ToolContext
    ) -> tuple[WindowInfo, ...]:
        self.session(context)
        results: list[WindowInfo] = []
        scanned = 0

        def visit(handle: int, unused: object) -> bool:
            nonlocal scanned
            context.checkpoint()
            scanned += 1
            if scanned > 1024:
                raise DesktopError("target_limit")
            if self.gui.GetClassName(handle) != app.window_class:
                return True
            try:
                _, pid = self.process.GetWindowThreadProcessId(handle)
                if self._identity(pid)[0] != app.version.path:
                    return True  # Do not connect UIA to an unrelated application.
                window = self.window(handle, context)
            except DesktopError:
                return True  # Inaccessible/unrelated windows disclose nothing.
            if window.executable == app.version.path:
                if app.fixture_version and window.pid not in self._owned:
                    return True
                results.append(window)
            return len(results) < limit

        self.gui.EnumWindows(visit, None)
        context.checkpoint()
        return tuple(results)

    def _wrapper(self, window: WindowInfo, context: ToolContext) -> Any:
        current = self.window(window.handle, context)
        if (
            current.identity != window.identity
            or not current.visible
            or not current.enabled
            or current.minimized
        ):
            raise DesktopError("target_mismatch")
        return (
            self.uia.Desktop(backend="uia")
            .window(handle=window.handle)
            .wrapper_object()
        )

    def _matches(
        self,
        window: WindowInfo,
        automation_id: str,
        control_type: str,
        context: ToolContext,
    ) -> list[Any]:
        wrapper = self._wrapper(window, context)
        walker = self.uia_defs.IUIA().iuia.ControlViewWalker
        element = walker.GetFirstChildElement(wrapper.element_info.element)
        matches: list[Any] = []
        # Direct children only. Never request an entire descendant tree or text dump.
        for _ in range(64):
            context.checkpoint()
            if not element:
                return matches
            child = self.uia_wrapper.UIAWrapper(self.uia_info.UIAElementInfo(element))
            info = child.element_info
            if (
                info.automation_id == automation_id
                and info.control_type == control_type
            ):
                if (
                    child.process_id() != window.pid
                    or child.top_level_parent().handle != window.handle
                ):
                    raise DesktopError("target_mismatch")
                matches.append(child)
                if len(matches) > 1:
                    return matches
            element = walker.GetNextSiblingElement(element)
        if element:
            raise DesktopError("target_limit")
        return matches

    def _info(self, wrapper: Any) -> ControlInfo:
        element = wrapper.element_info.element
        editable = False
        if (
            wrapper.element_info.control_type == "Edit"
            and not element.CurrentIsPassword
        ):
            editable = not bool(wrapper.iface_value.CurrentIsReadOnly)
        return ControlInfo(
            tuple(element.GetRuntimeId()),
            wrapper.element_info.automation_id,
            wrapper.element_info.control_type,
            wrapper.is_enabled(),
            wrapper.is_visible(),
            bool(element.CurrentIsPassword),
            editable,
        )

    def controls(
        self, window: WindowInfo, rule: ControlRule, context: ToolContext
    ) -> tuple[ControlInfo, ...]:
        return tuple(
            self._info(c)
            for c in self._matches(
                window, rule.automation_id, rule.control_type, context
            )
        )

    @contextmanager
    def _pin(self, path: Path) -> Iterator[None]:
        # Deny write/delete sharing through launch. Native loader may still read/execute.
        handle = self.files.CreateFile(str(path), 0x80000000, 1, None, 3, 0x80, None)
        try:
            yield
        finally:
            handle.Close()

    def launch(self, app: DesktopApplication, context: ToolContext) -> tuple[int, str]:
        self.session(context)
        if not app.multiple_instances and self._already_running(app, context):
            raise DesktopError("already_running")
        with ExitStack() as stack:
            stack.enter_context(self._pin(app.version.path))
            if app.fixture_version:
                stack.enter_context(self._pin(app.fixture_version.path))
            app.revalidate()
            context.checkpoint()
            try:
                process = subprocess.Popen(
                    [str(app.version.path), *app.arguments],
                    shell=False,
                    cwd=app.version.path.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                # Retain the actual process object, preventing PID-only ownership claims.
                self._owned[process.pid] = ("", process)
                executable, created = self._identity(process.pid)
                self._owned[process.pid] = (created, process)
                if executable != app.version.path:
                    raise DesktopError("target_mismatch")
            except DesktopError:
                raise
            except Exception:
                raise DesktopError("launch_failed") from None
        context.checkpoint()
        return process.pid, created

    def _already_running(self, app: DesktopApplication, context: ToolContext) -> bool:
        # Interpreter exceptions are restricted to our retained fixture children.
        pids = (
            tuple(self._owned)
            if app.fixture_version
            else tuple(self.process.EnumProcesses())
        )
        if len(pids) > 4096:
            raise DesktopError("target_limit")
        for pid in pids:
            context.checkpoint()
            if pid == os.getpid():
                continue
            try:
                with _handle(self.api.OpenProcess(0x1000, False, pid)) as process:
                    if self._image_path(process) == app.version.path:
                        return True
            except DesktopError:
                continue
            except self.api.error as error:
                if error.winerror not in (5, 6, 87):  # denied/closed/invalid PID
                    raise DesktopError("launch_failed") from None
                continue
            except OSError:
                continue  # Exited/inaccessible unrelated processes disclose nothing.
        return False

    def alive(self, pid: int, created: str, context: ToolContext) -> bool:
        context.checkpoint()
        try:
            return self._identity(pid)[1] == created
        except DesktopError:
            return False

    def focused(self, window: WindowInfo, context: ToolContext) -> bool:
        self._wrapper(window, context)
        return bool(self.gui.GetForegroundWindow() == window.handle)

    def act(
        self,
        operation: str,
        window: WindowInfo,
        rule: ControlRule | None,
        control: ControlInfo | None,
        text: str | None,
        context: ToolContext,
    ) -> None:
        self.session(context)
        wrapper = self._wrapper(window, context)
        if operation == "focus":
            context.checkpoint()
            # Native focus only; pywinauto.set_focus can generate global Alt input.
            if self.gui.GetForegroundWindow() != window.handle:
                try:
                    self.gui.SetForegroundWindow(window.handle)
                except Exception:
                    raise DesktopError("focus_mismatch") from None
            return
        assert rule is not None and control is not None
        matches = self._matches(window, rule.automation_id, rule.control_type, context)
        if len(matches) != 1:
            raise DesktopError("ambiguous_target" if matches else "target_not_found")
        wrapper = matches[0]
        if (
            self._info(wrapper) != control
            or control.password
            or not control.enabled
            or not control.visible
        ):
            raise DesktopError("target_mismatch")
        if not self.focused(window, context):
            raise DesktopError("focus_mismatch")
        context.checkpoint()
        # Call semantic interfaces directly: higher-level set_edit_text logs payloads,
        # and invoke() can create an abandoned worker thread on provider deadlocks.
        if operation == "text" and control.editable and text is not None:
            wrapper.iface_value.SetValue(text)
        elif operation == "click":
            wrapper.iface_invoke.Invoke()
        else:
            raise DesktopError("unsafe_target")
        context.checkpoint()

    def verify(
        self,
        operation: str,
        window: WindowInfo,
        rule: ControlRule | None,
        text: str | None,
        context: ToolContext,
    ) -> bool:
        self.session(context)
        if operation == "focus":
            return self.focused(window, context)
        assert rule is not None
        if operation == "text":
            matches = self._matches(
                window, rule.automation_id, rule.control_type, context
            )
            if len(matches) != 1 or self._info(matches[0]).password:
                return False
            return bool(matches[0].iface_value.CurrentValue == text)
        matches = self._matches(window, rule.status_id, "Text", context)
        return bool(
            len(matches) == 1 and matches[0].element_info.name == rule.expected_status
        )

    def close_owned(self) -> None:
        """Demo cleanup only: terminate retained child handles, never a looked-up PID."""
        for _, process in self._owned.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self._owned.clear()
