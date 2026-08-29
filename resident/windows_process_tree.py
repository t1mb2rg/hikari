from __future__ import annotations

from collections.abc import Mapping
import os


class WindowsProcessTreeUnavailable(RuntimeError):
    """Raised when Windows process-tree inspection is requested off Windows."""


def ordered_process_tree(
    root_pid: int,
    parent_by_pid: Mapping[int, int],
) -> list[int]:
    """Return one rooted process tree in parent-first order.

    Parent-first is intentional for Hikari shutdown. The resident owns restart
    supervisors; terminating it before integration descendants prevents a child
    from being recreated while the shutdown sweep is still in progress.
    """

    root = int(root_pid)
    if root <= 0:
        raise ValueError("root_pid must be a positive integer")

    children: dict[int, list[int]] = {}
    for raw_pid, raw_parent in parent_by_pid.items():
        pid = int(raw_pid)
        parent = int(raw_parent)
        if pid <= 0 or parent <= 0 or pid == parent:
            continue
        children.setdefault(parent, []).append(pid)

    result: list[int] = []
    queue = [root]
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        queue.extend(sorted(children.get(pid, ())))
    return result


def snapshot_windows_process_tree(root_pid: int) -> list[int]:
    """Snapshot descendants of ``root_pid`` using Toolhelp32.

    The snapshot is taken before shutdown begins so later parent termination and
    Windows re-parenting cannot hide already-owned descendants from the sweep.
    """

    if os.name != "nt":
        raise WindowsProcessTreeUnavailable(
            "Windows process-tree inspection is only available on Windows"
        )

    import ctypes
    from ctypes import wintypes

    th32cs_snapprocess = 0x00000002
    invalid_handle_value = ctypes.c_void_p(-1).value
    max_path = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * max_path),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if snapshot == invalid_handle_value:
        error = ctypes.get_last_error()
        raise OSError(error, "failed to snapshot Windows process tree")

    parent_by_pid: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return ordered_process_tree(root_pid, parent_by_pid)
