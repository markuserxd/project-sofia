"""Native-boundary security checks with fake OS/UIA objects; no desktop access."""

import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from desktop_helpers import approve, call, preview, setup

from sofia.security import Cancellation
from sofia.tools import ToolContext
from sofia.tools.desktop_backend import DesktopError
from sofia.tools.windows_desktop import WindowsDesktopBackend


def context():
    return ToolContext(Cancellation(), 100.0, lambda: 0.0)


def native():
    backend = WindowsDesktopBackend.__new__(WindowsDesktopBackend)
    backend.session = Mock()
    return backend


def test_launch_uses_only_fixed_configuration_and_no_shell(tmp_path, monkeypatch):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    backend._owned = {}
    backend._already_running = Mock(return_value=False)
    backend._pin = Mock(side_effect=lambda path: nullcontext())
    backend._identity = Mock(return_value=(app.version.path, "created"))
    child = SimpleNamespace(pid=42)
    create = Mock(return_value=child)
    monkeypatch.setattr("sofia.tools.windows_desktop.subprocess.Popen", create)
    assert backend.launch(app, context()) == (42, "created")
    args, kwargs = create.call_args
    assert args == ([str(app.version.path), *app.arguments],)
    assert kwargs["shell"] is False and kwargs["cwd"] == app.version.path.parent
    assert kwargs["close_fds"] is True
    assert backend._owned[42] == ("created", child)


def test_existing_process_without_window_blocks_launch(tmp_path, monkeypatch):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    backend._owned = {}
    backend.process = SimpleNamespace(EnumProcesses=lambda: [123])
    handle = Mock()
    backend.api = SimpleNamespace(OpenProcess=lambda *args: handle)
    backend._image_path = Mock(return_value=app.version.path)
    create = Mock()
    monkeypatch.setattr("sofia.tools.windows_desktop.subprocess.Popen", create)
    with pytest.raises(DesktopError, match="already_running"):
        backend.launch(app, context())
    create.assert_not_called()
    handle.Close.assert_called_once()


def test_inaccessible_unrelated_process_does_not_hide_existing_target(tmp_path):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    backend._owned = {}
    backend.process = SimpleNamespace(EnumProcesses=lambda: [111, 123])

    class AccessDenied(Exception):
        winerror = 5

    handle = Mock()

    def open_process(access, inherit, pid):
        if pid == 111:
            raise AccessDenied()
        return handle

    backend.api = SimpleNamespace(OpenProcess=open_process, error=AccessDenied)
    backend._image_path = Mock(return_value=app.version.path)
    assert backend._already_running(app, context())
    handle.Close.assert_called_once()


def test_discovery_filters_process_before_connecting_to_uia(tmp_path):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    backend.gui = SimpleNamespace(
        GetClassName=lambda handle: app.window_class,
        EnumWindows=lambda visit, arg: [visit(handle, arg) for handle in (10, 11)],
    )
    backend.process = SimpleNamespace(
        GetWindowThreadProcessId=lambda handle: (1, handle)
    )
    backend._identity = lambda pid: (
        app.version.path if pid == 10 else tmp_path / "unrelated.exe",
        "created",
    )
    backend.window = Mock(return_value=fake.rows[0])
    ctx = context()
    assert backend.windows(app, 16, ctx) == (fake.rows[0],)
    backend.window.assert_called_once_with(10, ctx)


@pytest.mark.parametrize(
    "level,user,session",
    [(0x3000, "owner", 1), (0x2000, "other", 1), (0x2000, "owner", 2)],
)
def test_identity_rejects_elevation_other_user_and_session(level, user, session):
    backend = native()
    handle = Mock()
    backend.api = SimpleNamespace(
        OpenProcess=lambda *args: handle, GetCurrentProcess=lambda: "self"
    )
    backend._token = lambda p: ("owner", 0x2000) if p == "self" else (user, level)
    backend.ts = SimpleNamespace(
        ProcessIdToSessionId=lambda pid: session if pid == 123 else 1
    )
    with pytest.raises(DesktopError, match="unsafe_target"):
        backend._identity(123)
    handle.Close.assert_called_once()


@pytest.mark.parametrize(
    "locked,active,integrity",
    [(True, True, 0x2000), (False, False, 0x2000), (False, True, 0x3000)],
)
def test_session_rejects_lock_disconnection_and_elevated_controller(
    locked, active, integrity
):
    backend = native()
    del backend.session  # Exercise the actual implementation with synthetic OS APIs.
    desktop = Mock()
    backend.api = SimpleNamespace(
        GetCurrentProcess=lambda: None, GetCurrentThreadId=lambda: 7
    )
    backend.ts = SimpleNamespace(
        ProcessIdToSessionId=lambda pid: 1,
        WTSConnectState=8,
        WTSActive=0,
        WTSQuerySessionInformation=lambda *args: 0 if active else 4,
    )
    backend.station = SimpleNamespace(
        GetProcessWindowStation=lambda: "station",
        GetUserObjectInformation=lambda h, index: (
            "WinSta0" if h == "station" else "Winlogon" if locked else "Default"
        ),
        OpenInputDesktop=lambda *args: desktop,
        GetThreadDesktop=lambda *args: "thread",
    )
    backend._token = lambda p: ("owner", integrity)
    with pytest.raises(DesktopError, match="unsupported_session"):
        backend.session(context())


def test_native_semantic_text_does_not_use_keyboard_clipboard_or_payload_logging(
    tmp_path,
):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    control = fake.fields["entry"][0]
    wrapper = SimpleNamespace(iface_value=SimpleNamespace(SetValue=Mock()))
    backend._wrapper = Mock(return_value=wrapper)
    backend._matches = Mock(return_value=[wrapper])
    backend._info = Mock(return_value=control)
    backend.focused = Mock(return_value=True)
    backend.act(
        "text", fake.rows[0], app.controls[0], control, "literal ^%{ENTER}", context()
    )
    wrapper.iface_value.SetValue.assert_called_once_with("literal ^%{ENTER}")
    backend.focused.return_value = False
    with pytest.raises(DesktopError, match="focus_mismatch"):
        backend.act("text", fake.rows[0], app.controls[0], control, "other", context())
    assert wrapper.iface_value.SetValue.call_count == 1


def test_native_control_change_at_last_boundary_blocks_input(tmp_path):
    adapter, registry, run, fake, app, wid, clock = setup(tmp_path)
    backend = native()
    control = fake.fields["button"][0]
    wrapper = SimpleNamespace(iface_invoke=SimpleNamespace(Invoke=Mock()))
    backend._wrapper = Mock(return_value=wrapper)
    backend._matches = Mock(return_value=[wrapper])
    backend._info = Mock(return_value=replace(control, password=True))
    with pytest.raises(DesktopError, match="target_mismatch"):
        backend.act("click", fake.rows[0], app.controls[1], control, None, context())
    wrapper.iface_invoke.Invoke.assert_not_called()


def test_unknown_desktop_error_message_is_never_exposed(tmp_path, caplog):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    caplog.set_level("INFO")
    secret = "private-untrusted-error-payload"
    backend.hook = lambda stage: (_ for _ in ()).throw(DesktopError(secret))
    assert call(registry, "discover", app_id="fixture")["code"] == "desktop_failed"
    assert secret not in caplog.text


def test_package_import_does_not_load_windows_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, sofia, sofia.tools, sofia.tools.desktop; "
            "assert not {'pywinauto', 'win32gui', 'pyautogui'} & sys.modules.keys()",
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0


def test_real_non_windows_invocation_fails_closed(tmp_path, monkeypatch):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    adapter._backend = None
    monkeypatch.setattr(sys, "platform", "linux")
    assert call(registry, "discover", app_id="fixture") == {
        "status": "error",
        "code": "unsupported_platform",
    }


def test_accounting_block_appearing_during_observation_prevents_input(tmp_path):
    adapter, registry, run, backend, app, wid, clock = setup(tmp_path)
    pid = preview(registry, wid)
    approve(adapter, run, pid)
    from sofia.security import AccountingBlocked

    def block(stage):
        if stage == "focused":
            reservation = run.budget.reserve(
                estimated_tokens=1, estimated_usd=Decimal(0)
            )
            with pytest.raises(AccountingBlocked):
                run.budget.settle(reservation, actual_tokens=None, actual_usd=None)

    backend.hook = block
    with pytest.raises(AccountingBlocked):
        call(registry, "interact", preview_id=pid)
    assert not backend.actions
