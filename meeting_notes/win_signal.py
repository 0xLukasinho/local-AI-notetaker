"""Windows-only process-group signal.

Imported only after a sys.platform == 'win32' check. CTRL_BREAK_EVENT is 1.
"""


def send_ctrl_break(pid):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctrl_break_event = 1
    kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    return bool(kernel32.GenerateConsoleCtrlEvent(ctrl_break_event, int(pid)))
