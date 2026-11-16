"""Screen & System Audio Recording status for Meeting Audio Capture.

The check runs inside the helper, launched as its own responsible process,
so the answer is about that app and not about Terminal or Python.
"""

import json
import sys

from meeting_notes.macos_capture import find_capture_helper
from meeting_notes.responsible_spawn import run_responsible


def interpret_screen_report(payload):
    if isinstance(payload, dict) and payload.get("screen_recording") is True:
        return "granted"
    if isinstance(payload, dict) and payload.get("screen_recording") is False:
        return "missing"
    return "unknown"


def permission_warning(status):
    """One line when the helper cannot record other people's audio. Empty otherwise."""
    if status != "missing":
        return ""
    return (
        "Screen & System Audio Recording is not granted to Meeting Audio Capture. "
        "Meet detection uses the Chrome extension and does not need this permission. "
        "Other participants' audio and Slack window titles stay unavailable until you run "
        "`meeting-notes permissions request` and click Allow for Meeting Audio Capture."
    )


def screen_permission_for_platform(platform=None):
    if (platform or sys.platform) != "darwin":
        return "not-applicable"
    return None


def read_screen_permission(helper=None, run=None):
    """Return granted, missing, unknown, or not-applicable."""
    forced = screen_permission_for_platform()
    if forced:
        return forced
    helper = find_capture_helper() if helper is None else helper
    if not helper:
        return "missing"
    argv = [helper, "permissions"]
    if run is None:
        code, text = run_responsible(argv, timeout=8)
    else:
        code, text = run(argv)
    if code not in (0, None):
        return "unknown"
    try:
        payload = json.loads(text) if str(text).strip() else {}
    except json.JSONDecodeError:
        return "unknown"
    return interpret_screen_report(payload)


def request_screen_permission(helper=None, run=None):
    """Ask macOS to show the prompt for Meeting Audio Capture. Returns the status."""
    forced = screen_permission_for_platform()
    if forced:
        return forced
    helper = find_capture_helper() if helper is None else helper
    if not helper:
        return "missing"
    argv = [helper, "permissions", "--request"]
    if run is None:
        code, text = run_responsible(argv, timeout=120)
    else:
        code, text = run(argv)
    if code not in (0, None):
        return "unknown"
    try:
        payload = json.loads(text) if str(text).strip() else {}
    except json.JSONDecodeError:
        return "unknown"
    return interpret_screen_report(payload)
