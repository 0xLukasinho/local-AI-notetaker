"""macOS window list and microphone probe for the call watcher.

Both read the Swift helper (``meeting-audio-capture probe``). The helper is
the process that macOS will ask for Screen & System Audio Recording, because
window titles of other apps are blank without it, and for the microphone
list. This module does not import Windows libraries.

The functions return the same shapes the Windows probes return: a list of
``WindowInfo`` and a ``MicStatus``. A missing helper is an empty list and a
status whose ``notice`` says to run the setup script. It does not raise, so
one failed probe cannot start a recording.
"""

import time

from meeting_notes.macos_capture import (
    find_capture_helper,
    mic_status_from_probe,
    run_probe,
    titles_need_permission,
    windows_from_probe,
)


_CACHE_SECONDS = 1.0
_cache = {"at": 0.0, "payload": None}
_title_warning_sent = False


def _read(now=None, runner=None, helper=None):
    """One probe shared by the window list and the mic check in the same poll."""
    current = time.monotonic() if now is None else float(now)
    cached = _cache["payload"]
    if cached is not None and current - float(_cache["at"]) < _CACHE_SECONDS:
        return cached
    if helper is None:
        helper = find_capture_helper(purpose="probe")
    payload = run_probe(helper, runner)
    _cache["at"] = current
    _cache["payload"] = payload
    return payload


def list_windows():
    """Top-level windows as ``WindowInfo(title, owner)``."""
    global _title_warning_sent
    payload = _read()
    if titles_need_permission(payload) and not _title_warning_sent:
        _title_warning_sent = True
        _warn_titles()
    return windows_from_probe(payload)


def probe_microphone(marker="chrome.exe"):
    """Whether `marker` (``chrome.exe`` or ``slack.exe``) is using the mic."""
    return mic_status_from_probe(_read(), marker)


def _warn_titles():
    try:
        import logging

        logging.getLogger("meeting_notes.watch").warning(
            "Chrome and Slack window titles are blank. Meet calls are detected "
            "from the Chrome extension and do not need this. Slack window titles "
            "and other participants' audio need Screen & System Audio Recording "
            "for Meeting Audio Capture. Run `meeting-notes permissions request` "
            "and click Allow."
        )
    except Exception:
        pass


def reset_probe_cache():
    """Test hook. Production code does not call this."""
    global _title_warning_sent
    _cache["at"] = 0.0
    _cache["payload"] = None
    _title_warning_sent = False
