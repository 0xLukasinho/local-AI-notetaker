"""macOS capture helper: where it lives, and how a probe result is read.

The helper is a small Swift program built by ``scripts/setup_macos.sh``.
Recording still goes through ``recorder_mac``: when the helper is installed
it captures system audio plus the microphone, and otherwise the original
ffmpeg AVFoundation recorder is used.

Nothing in this module starts a recording. Callers pass a ``runner`` in tests.
"""

import json
import os
import subprocess

from meeting_notes.meet_detect import MicStatus, WindowInfo
from meeting_notes.platform_match import process_matches


HELPER_ENV = "MEETING_NOTES_CAPTURE_HELPER"
CAPTURE_ENV = "MEETING_NOTES_CAPTURE"
DEVICE_ENV = "MEETING_NOTES_AVFOUNDATION_DEVICE"
HELPER_APP = "MeetingAudioCapture.app"
HELPER_BINARY = "meeting-audio-capture"


def capture_helper_candidates(home=None):
    """Absolute paths checked, in order, after the environment override."""
    root = home or os.path.expanduser("~")
    bindir = os.path.join(root, ".meeting-notes", "bin")
    bundled = os.path.join(
        bindir,
        HELPER_APP,
        "Contents",
        "MacOS",
        HELPER_BINARY,
    )
    return (bundled, os.path.join(bindir, HELPER_BINARY))


def find_capture_helper(environ=None, home=None):
    """Return the helper executable, or None when it has not been built."""
    environ = os.environ if environ is None else environ
    override = (environ.get(HELPER_ENV) or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    for path in capture_helper_candidates(home):
        if os.path.isfile(path):
            return path
    return None


def capture_backend_for(environ, helper_exists):
    """``helper`` or ``avfoundation``.

    ``auto`` (the default) uses the helper when it is installed and the
    original single-device ffmpeg recorder otherwise. ``avfoundation``,
    ``ffmpeg``, and ``blackhole`` all keep that original recorder.
    ``blackhole`` does not install anything; it is the name for pointing the
    AVFoundation device index at a virtual device you already set up.
    """
    mode = (environ.get(CAPTURE_ENV) or "auto").strip().lower()
    if mode in ("avfoundation", "ffmpeg", "blackhole"):
        return "avfoundation"
    if mode == "helper":
        return "helper"
    if helper_exists:
        return "helper"
    return "avfoundation"


def avfoundation_device(environ, default="1"):
    value = (environ.get(DEVICE_ENV) or "").strip()
    return value or str(default)


def helper_record_argv(helper, output_path):
    return [helper, "record", "--output", output_path]


def helper_probe_argv(helper):
    return [helper, "probe"]


def helper_wait_argv(helper):
    return [helper, "wait-session"]


def parse_probe_json(text):
    """Parse helper stdout. A bad payload becomes an empty probe with an error."""
    try:
        payload = json.loads(text) if text and str(text).strip() else {}
    except json.JSONDecodeError:
        return {"windows": [], "inputs": [], "error": "probe output was not JSON"}
    if not isinstance(payload, dict):
        return {"windows": [], "inputs": [], "error": "probe output was not an object"}
    return payload


def run_probe(helper, runner):
    """Run `helper probe` and return the JSON object. `runner` is subprocess.run."""
    if not helper:
        return {"windows": [], "inputs": [], "error": "helper-missing"}
    try:
        completed = runner(
            helper_probe_argv(helper),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"windows": [], "inputs": [], "error": "probe timed out"}
    except OSError as exc:
        return {"windows": [], "inputs": [], "error": str(exc)}
    return parse_probe_json(getattr(completed, "stdout", "") or "")


def windows_from_probe(payload):
    """WindowInfo list. Owner name is the process name the detectors match."""
    windows = []
    for item in payload.get("windows") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        owner = str(item.get("owner") or "")
        if not title and not owner:
            continue
        windows.append(WindowInfo(title, owner))
    return windows


def _input_identity(item):
    name = str(item.get("name") or "")
    bundle = str(item.get("bundle_id") or "")
    return (name + " " + bundle).strip()


def mic_status_from_probe(payload, marker):
    """MicStatus for one marker. Core Audio is the only macOS probe.

    The Windows registry and WASAPI fields stay empty so the log does not
    claim those probes ran.
    """
    error = payload.get("error") or ""
    if error == "helper-missing":
        return MicStatus(
            False,
            (),
            notice=(
                "macOS audio helper is not installed. "
                "Run scripts/setup_macos.sh so Meet and Slack can be detected."
            ),
        )
    if error:
        return MicStatus(False, (), notice="macOS audio probe failed (%s)" % error)
    if payload.get("input_available") is False:
        return MicStatus(
            False,
            (),
            notice=(
                "Core Audio did not report which app is using the microphone. "
                "Google Meet will be detected from the Chrome window title alone. "
                "Slack Huddles wait until the microphone probe works."
            ),
            available=False,
        )
    in_use = False
    for item in payload.get("inputs") or []:
        if not isinstance(item, dict) or not item.get("input_running"):
            continue
        if process_matches(_input_identity(item), marker, platform="darwin"):
            in_use = True
            break
    if in_use:
        return MicStatus(True, ("coreaudio",))
    return MicStatus(False, ())


def titles_need_permission(payload):
    """True when windows were listed and every title was blank.

    macOS omits other apps' window titles until Screen & System Audio
    Recording is allowed. A machine with no open windows is not that case.
    """
    windows = [item for item in (payload.get("windows") or []) if isinstance(item, dict)]
    if not windows:
        return False
    return all(not str(item.get("title") or "").strip() for item in windows)
