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


# Built from source by scripts/install_macos.sh and signed with a local,
# stable code-signing identity, so its TCC grants survive rebuilds. Used for
# recording once `meeting-notes selftest --verify-signed` proved it captures
# real system audio (that writes SIGNED_MARKER); used for window probing only
# when the marker also records a Screen Recording grant.
SIGNED_HELPER_APP = "MeetingAudioCapture-signed.app"
SIGNED_MARKER = ".signed-helper-verified.json"


def signed_helper_path(home=None):
    root = home or os.path.expanduser("~")
    return os.path.join(root, ".meeting-notes", "bin", SIGNED_HELPER_APP, "Contents", "MacOS", HELPER_BINARY)


def signed_helper_status(home=None):
    """The verification marker as a dict, or {} when not verified."""
    import json

    root = home or os.path.expanduser("~")
    marker = os.path.join(root, ".meeting-notes", "bin", SIGNED_MARKER)
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_signed_helper(path, home=None):
    return bool(path) and os.path.abspath(path) == os.path.abspath(signed_helper_path(home))


def find_capture_helper(environ=None, home=None, purpose="record"):
    """Return the helper executable, or None when it has not been built.

    `purpose` is "record" or "probe". The signed helper is preferred once
    verified for that purpose; otherwise the older ad-hoc helper is used.
    """
    environ = os.environ if environ is None else environ
    override = (environ.get(HELPER_ENV) or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    signed = signed_helper_path(home)
    status = signed_helper_status(home)
    if os.path.isfile(signed) and status.get("record_ok"):
        if purpose == "record" or status.get("screen_recording"):
            return signed
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


def helper_record_argv(helper, output_path, self_path=None, others_path=None):
    argv = [helper, "record", "--output", output_path]
    if self_path and others_path:
        argv.extend(["--self", self_path, "--others", others_path])
    return argv


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


def run_probe(helper, runner=None):
    """Run `helper probe` and return the JSON object.

    The default runner launches the helper as its own responsible process so
    Screen Recording is attributed to Meeting Audio Capture, not to Python.
    Tests pass `runner(argv) -> (returncode, stdout)`.
    """
    if not helper:
        return {"windows": [], "inputs": [], "error": "helper-missing"}
    try:
        if runner is None:
            from meeting_notes.responsible_spawn import run_responsible

            code, text = run_responsible(helper_probe_argv(helper), timeout=8)
        else:
            code, text = runner(helper_probe_argv(helper))
    except subprocess.TimeoutExpired:
        return {"windows": [], "inputs": [], "error": "probe timed out"}
    except OSError as exc:
        return {"windows": [], "inputs": [], "error": str(exc)}
    if code not in (0, None) and not str(text).strip():
        return {"windows": [], "inputs": [], "error": "probe exited %s" % code}
    return parse_probe_json(text or "")


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


def retire_shim(home=None):
    """Move the legacy DYLD shim aside and point the helper symlink at the signed app.

    Safe to call more than once. Returns the backup path, or None when there
    was no shim directory to move.
    """
    import time

    root = os.path.join(home or os.path.expanduser("~"), ".meeting-notes")
    shim = os.path.join(root, "shim")
    moved = None
    if os.path.isdir(shim):
        backups = os.path.join(root, "backups")
        os.makedirs(backups, exist_ok=True)
        dest = os.path.join(backups, "shim-" + time.strftime("%Y%m%d%H%M%S"))
        os.rename(shim, dest)
        moved = dest
    signed = signed_helper_path(home)
    link = os.path.join(root, "bin", "meeting-audio-capture")
    if os.path.isfile(signed):
        if os.path.islink(link) or os.path.isfile(link):
            os.remove(link)
        os.symlink(signed, link)
    return moved

def fallback_probe_helper(exclude=None, home=None):
    """Older capture helper candidate when the signed helper lacks Screen Recording."""
    excluded = os.path.abspath(exclude) if exclude else None
    for path in capture_helper_candidates(home):
        if os.path.isfile(path) and (excluded is None or os.path.abspath(path) != excluded):
            return path
    return None


def refresh_signed_screen_recording(home=None, run=None):
    """Re-check the signed helper's Screen Recording grant via disclaimed spawn.

    Fixes a stale marker written before responsible_spawn was used (which could
    inherit Terminal's grant). Never touches record_ok. Returns the updated dict
    or {} when no marker exists.
    """
    status = signed_helper_status(home)
    if not status:
        return {}
    signed = signed_helper_path(home)
    if not os.path.isfile(signed):
        return status
    if run is None:
        from meeting_notes.responsible_spawn import run_responsible

        code, text = run_responsible([signed, "permissions"], timeout=8)
    else:
        code, text = run([signed, "permissions"])
    if code not in (0, None):
        return status
    try:
        payload = json.loads((text or "").strip() or "{}")
    except json.JSONDecodeError:
        return status
    if "screen_recording" not in payload:
        return status
    actual = bool(payload.get("screen_recording"))
    if bool(status.get("screen_recording")) == actual:
        return status
    updated = dict(status)
    updated["screen_recording"] = actual
    root = home or os.path.expanduser("~")
    marker = os.path.join(root, ".meeting-notes", "bin", SIGNED_MARKER)
    from meeting_notes.atomic_io import atomic_write_json

    atomic_write_json(marker, updated)
    return updated
