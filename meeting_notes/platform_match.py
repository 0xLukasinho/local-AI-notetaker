"""Match a process name against the configured microphone marker.

Windows process names stay ``chrome.exe`` and ``slack.exe``. On macOS the
same config values also match Google Chrome and Slack, because that is what
those apps are called there. On every other platform the comparison is the
same substring check the Windows probes already used.
"""

import sys


# Applied only when the platform is darwin. The Windows names still match
# first, via the substring check, so a Windows process list is unchanged.
_DARWIN_ALIASES = {
    "chrome.exe": ("google chrome", "com.google.chrome"),
    "slack.exe": ("slack", "com.tinyspeck.slackmacgap"),
}


def process_matches(process_name, marker, platform=None):
    """True when `process_name` is the app `marker` refers to.

    `marker` is the config value (``chrome.exe`` by default). Matching is
    case-insensitive. A marker that is already a substring matches on every
    platform. macOS-only aliases are consulted after that.
    """
    name = (process_name or "").lower()
    needle = (marker or "").lower()
    if not needle or not name:
        return False
    if needle in name:
        return True
    if (platform or sys.platform) != "darwin":
        return False
    for alias in _DARWIN_ALIASES.get(needle, ()):
        if alias in name:
            return True
    return False

# Human labels for logs. The config/marker values stay chrome.exe / slack.exe.
_DISPLAY = {
    "chrome.exe": "Google Chrome",
    "slack.exe": "Slack",
}


def display_label(marker, platform=None):
    """A readable app name for logs. Markers themselves stay Windows-style."""
    needle = (marker or "").lower()
    if (platform or sys.platform) == "darwin" and needle in _DISPLAY:
        return _DISPLAY[needle]
    return marker or ""
