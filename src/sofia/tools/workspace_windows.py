"""Small Win32 handle adapter, imported only on Windows; no native dependency."""

import ctypes
import os
import struct
import sys
from ctypes import wintypes
from pathlib import Path

from sofia.tools.workspace_paths import WorkspaceError


def check_volume(root: Path) -> None:
    if sys.platform != "win32":
        raise WorkspaceError("unsupported_platform")
    # Reject UNC, device/extended namespaces, SUBST/network and non-NTFS volumes.
    if len(root.drive) != 2 or root.drive[1] != ":" or "~" in str(root):
        raise WorkspaceError("unsupported_filesystem")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    if drive_type(root.anchor) != 3:  # DRIVE_FIXED
        raise WorkspaceError("unsupported_filesystem")
    volume = kernel.GetVolumeInformationW
    volume.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    volume.restype = wintypes.BOOL
    filesystem = ctypes.create_unicode_buffer(32)
    if (
        not volume(root.anchor, None, 0, None, None, None, filesystem, 32)
        or filesystem.value != "NTFS"
    ):
        raise WorkspaceError("unsupported_filesystem")
    query = kernel.QueryDosDeviceW
    query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query.restype = wintypes.DWORD
    mapping = ctypes.create_unicode_buffer(1024)
    if not query(root.drive, mapping, 1024) or not mapping.value.startswith(
        "\\Device\\HarddiskVolume"
    ):
        raise WorkspaceError("unsupported_filesystem")


def open_file(path: Path, *, directory: bool) -> int:
    if sys.platform != "win32":
        raise WorkspaceError("unsupported_platform")
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Request read access (not just attributes) so NTFS enforces share denial;
    # attribute-only handles do not pin directories against rename/delete.
    # Regular readers deny concurrent ordinary writes and deletes while open.
    handle = create(
        str(path),
        0x80000000,
        3 if directory else 1,
        None,
        3,
        0x02200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("workspace_open_failed")
    try:
        # Reject ALL reparse tags before converting/using the handle. File info
        # must be obtained from the handle, not a racy second path lookup.
        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        query.restype = wintypes.BOOL
        info = AttributeTag()
        if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("workspace_stat_failed")
        if info.attributes & 0x400:
            raise WorkspaceError("link_blocked")
        if not directory:
            streams = ctypes.create_string_buffer(65536)
            if not query(handle, 7, streams, len(streams)):
                raise WorkspaceError("unsupported_file_type")
            # FILE_STREAM_INFO: offset, name length, two LARGE_INTEGERs, name.
            # More than the unnamed data stream would make text-only previews
            # incomplete and replacement/deletion could silently lose that data.
            next_offset, name_length = struct.unpack_from("<II", streams.raw)
            if (
                next_offset
                or name_length != 14
                or streams.raw[24:38] != "::$DATA".encode("utf-16-le")
            ):
                raise WorkspaceError("unsupported_file_type")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


def copy_dacl(source: Path, destination: Path) -> None:
    """Keep replacement from widening an existing file's discretionary access.

    Caller pins parents and asserts exclusive access. Fail before publication if
    the descriptor cannot be copied; never mutate the source descriptor.
    """
    if sys.platform != "win32":
        raise WorkspaceError("unsupported_platform")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get = security.GetFileSecurityW
    get.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get.restype = wintypes.BOOL
    set_security = security.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get(str(source), 4, None, 0, ctypes.byref(needed))
    if not 1 <= needed.value <= 65536:
        raise WorkspaceError("metadata_unsupported")
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get(str(source), 4, descriptor, len(descriptor), ctypes.byref(needed)):
        raise WorkspaceError("metadata_unsupported")
    # Freeze the copied effective DACL instead of adding inherited parent ACEs.
    if not set_security(str(destination), 4 | 0x80000000, descriptor):
        raise WorkspaceError("metadata_unsupported")
