"""Synthetic Windows fixture using standard-library ctypes and native UIA controls.

Native Edit/Button/Static controls expose stable IDs to UIA (Tk widgets do not
reliably expose Value/Invoke patterns). No file, network, clipboard or shell I/O.
"""

import ctypes
import sys
from ctypes import wintypes


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows interactive session required")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("procedure", callback_type),
            ("class_extra", ctypes.c_int),
            ("window_extra", ctypes.c_int),
            ("instance", wintypes.HINSTANCE),
            ("icon", wintypes.HICON),
            ("cursor", wintypes.HANDLE),
            ("background", wintypes.HBRUSH),
            ("menu", wintypes.LPCWSTR),
            ("name", wintypes.LPCWSTR),
        ]

    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    user.RegisterClassW.restype = wintypes.ATOM
    user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    status = None
    count = 0

    @callback_type
    def procedure(handle, message, word, long):
        nonlocal count
        if message == 0x0111 and word & 0xFFFF == 1002 and word >> 16 == 0:
            count += 1
            user.SetWindowTextW(status, f"SOFIA count: {count}")
            return 0
        if message == 0x0010:
            user.DestroyWindow(handle)
            return 0
        if message == 0x0002:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(handle, message, word, long)

    instance = kernel.GetModuleHandleW(None)
    name = "SOFIA_Desktop_Fixture"
    cls = WindowClass(0, procedure, 0, 0, instance, None, None, 6, None, name)
    if not user.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    window = user.CreateWindowExW(
        0,
        name,
        "SOFIA synthetic desktop fixture",
        0x00CF0000,
        200,
        200,
        420,
        220,
        None,
        None,
        instance,
        None,
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    for kind, label, control_id, y, style in (
        ("Edit", "", 1001, 20, 0x00800000 | 0x0080),
        ("Button", "Increment synthetic counter", 1002, 65, 0),
        ("Static", "SOFIA count: 0", 1003, 115, 0),
    ):
        child = user.CreateWindowExW(
            0,
            kind,
            label,
            0x50000000 | style,
            20,
            y,
            350,
            30,
            window,
            control_id,
            instance,
            None,
        )
        if not child:
            raise ctypes.WinError(ctypes.get_last_error())
        if control_id == 1003:
            status = child
    user.ShowWindow(window, 5)
    message = wintypes.MSG()
    while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user.TranslateMessage(ctypes.byref(message))
        user.DispatchMessageW(ctypes.byref(message))


if __name__ == "__main__":
    main()
