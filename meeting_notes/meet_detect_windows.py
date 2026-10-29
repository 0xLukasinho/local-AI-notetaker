"""Windows implementations of the Meet window and microphone probes.

Imported by the watcher on Windows. The module itself imports cleanly on other
platforms; the functions raise if they are called there. Registry, Win32, and
WASAPI calls stay inside the functions so unit tests never need them.

The microphone is in use when either probe says so:

* the legacy consent-store registry (``LastUsedTimeStop == 0``), or
* an active WASAPI capture session on an input endpoint whose process name
  matches the marker (``pycaw`` + ``psutil``).

Windows 11 26H2 (build 26300) and some earlier 24H2/25H2 builds stop updating
``ConsentStore\\microphone\\NonPackaged`` and keep the history in
``CapabilityAccessManager.db`` instead. The registry probe is kept so older
builds still work. If ``pycaw`` is missing, the WASAPI probe is skipped.
"""

import os
import sys

from meeting_notes.meet_detect import WindowInfo, combine_mic_signals


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


def probe_microphone(marker="chrome.exe"):
    """OR the consent-store and WASAPI probes into a MicStatus.

    A failing probe is ``unavailable`` and does not hide the other one.
    ``notice`` tells the operator why a probe was skipped; the detector logs
    it once.
    """
    notices = []
    registry_entries = []
    registry_available = True
    try:
        registry_entries = read_microphone_entries()
    except Exception as exc:
        registry_available = False
        notices.append("Microphone consent-store probe failed (%s)" % exc)

    capture_sessions = []
    wasapi_available = True
    try:
        capture_sessions = read_wasapi_capture_sessions()
    except ImportError as exc:
        wasapi_available = False
        notices.append(
            "WASAPI microphone probe unavailable (%s); "
            "using the consent-store registry only" % exc
        )
    except Exception as exc:
        wasapi_available = False
        notices.append(
            "WASAPI microphone probe failed (%s); "
            "using the consent-store registry only" % exc
        )
    return combine_mic_signals(
        marker,
        registry_entries,
        capture_sessions,
        registry_available=registry_available,
        wasapi_available=wasapi_available,
        notice="; ".join(notices),
    )


def read_wasapi_capture_sessions():
    """Return ``(process_name, state)`` for sessions on active capture endpoints.

    Raises ImportError when pycaw is not installed, and RuntimeError when not
    on Windows. Other failures propagate so the caller can keep the registry
    probe.
    """
    _require_windows()
    _ensure_com()
    audio_utilities, control2_type, e_capture, active_state = _import_pycaw()
    devices = _enumerate_capture_devices(audio_utilities, e_capture, active_state)
    managers = _capture_managers(devices)
    return capture_sessions_from_managers(
        managers,
        _process_name,
        control2_type=control2_type,
    )


def _import_pycaw():
    try:
        from pycaw.pycaw import AudioUtilities, IAudioSessionControl2
        from pycaw.constants import DEVICE_STATE, EDataFlow
    except ImportError:
        try:
            from pycaw.utils import AudioUtilities
            from pycaw.api.audiopolicy import IAudioSessionControl2
            from pycaw.constants import DEVICE_STATE, EDataFlow
        except ImportError as exc:
            raise ImportError(
                "pycaw is not installed; pip install pycaw"
            ) from exc
    return (
        AudioUtilities,
        IAudioSessionControl2,
        EDataFlow.eCapture.value,
        DEVICE_STATE.ACTIVE.value,
    )


def _ensure_com():
    import comtypes

    try:
        comtypes.CoInitialize()
    except OSError:
        # Already initialized, including in a different apartment.
        return


def _enumerate_capture_devices(audio_utilities, e_capture, active_state):
    get_all = getattr(audio_utilities, "GetAllDevices", None)
    if get_all is not None:
        return get_all(e_capture, active_state) or []
    get_devices = getattr(audio_utilities, "GetDevices", None)
    if get_devices is not None:
        return get_devices(e_capture, active_state) or []
    raise RuntimeError("pycaw has no capture-device enumerator")


def _capture_managers(devices):
    """Session managers for each device. Raise if every device fails to open."""
    managers = []
    seen = 0
    for device in devices or []:
        seen += 1
        try:
            manager = device.AudioSessionManager
        except Exception:
            continue
        if manager is None:
            continue
        managers.append(manager)
    if seen and not managers:
        raise OSError("could not open a capture session manager")
    return managers


def capture_sessions_from_managers(managers, process_name_for_pid, control2_type=None):
    """Read ``(process_name, state)`` from session-manager-like objects.

    One bad device or session is skipped. ``process_name_for_pid`` is called
    only for a non-zero pid.
    """
    sessions = []
    for manager in managers or []:
        if manager is None:
            continue
        try:
            enumerator = manager.GetSessionEnumerator()
        except Exception:
            continue
        if enumerator is None:
            continue
        try:
            count = int(enumerator.GetCount())
        except Exception:
            continue
        for index in range(count):
            try:
                ctl = enumerator.GetSession(index)
            except Exception:
                continue
            if ctl is None:
                continue
            ctl2 = _as_control2(ctl, control2_type)
            try:
                pid = int(ctl2.GetProcessId())
                state = int(ctl2.GetState())
            except Exception:
                continue
            name = ""
            if pid:
                try:
                    name = process_name_for_pid(pid) or ""
                except Exception:
                    name = ""
            sessions.append((str(name), state))
    return sessions


def _as_control2(ctl, control2_type):
    """Prefer IAudioSessionControl2. GetSession often returns the base interface,
    which has GetState but not GetProcessId.
    """
    if control2_type is not None and hasattr(ctl, "QueryInterface"):
        try:
            upgraded = ctl.QueryInterface(control2_type)
        except Exception:
            upgraded = None
        if upgraded is not None:
            return upgraded
    return ctl


def _process_name(pid):
    if not pid:
        return ""
    import psutil

    try:
        return str(psutil.Process(pid).name() or "")
    except Exception:
        return ""


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
