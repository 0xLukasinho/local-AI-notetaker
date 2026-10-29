"""Windows implementations of the Meet window and microphone probes.

Imported by the watcher on Windows. The module itself imports cleanly on other
platforms; the functions raise if they are called there. Registry and Win32
calls stay inside the functions so unit tests never need them.
"""

import os
import sys

from meeting_notes.meet_detect import WindowInfo


_MIC_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone\NonPackaged"
)


def _require_windows():
    if sys.platform != "win32":
        raise RuntimeError("Meet window and microphone probes require Windows")


def read_microphone_entries():
    """Return ``(subkey, LastUsedTimeStop)`` from the mic consent store.

    Subkeys under NonPackaged are exe paths with ``#`` in place of backslashes,
    for example ``...#chrome.exe``. LastUsedTimeStop == 0 means that exe is
    using the microphone right now.
    """
    _require_windows()
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY)
    except OSError:
        return []
    entries = []
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(key, name)
            except OSError:
                continue
            try:
                try:
                    stop, _typ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
                except OSError:
                    continue
            finally:
                winreg.CloseKey(sub)
            entries.append((name, stop))
    finally:
        winreg.CloseKey(key)
    return entries


def list_top_level_windows():
    """Visible top-level windows as WindowInfo(title, process basename)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetParent(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append(
            WindowInfo(
                title=buf.value,
                process_name=_process_basename(kernel32, pid.value),
            )
        )
        return True

    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL
    if not user32.EnumWindows(callback, 0):
        err = ctypes.get_last_error()
        if err:
            raise OSError(err, "EnumWindows failed")
    return windows


def _process_basename(kernel32, pid):
    import ctypes
    from ctypes import wintypes

    process_query = 0x1000
    handle = kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
