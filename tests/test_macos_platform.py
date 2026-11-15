"""Platform behavior that the macOS watcher adds, and the Windows checks it must not change."""

import subprocess

import pytest

from meeting_notes.launchd_agent import (
    install_launch_agent,
    render_plist,
    supervise,
    uninstall_launch_agent,
    wait_for_session_event,
)
from meeting_notes.macos_capture import (
    capture_backend_for,
    helper_record_argv,
    mic_status_from_probe,
    parse_probe_json,
    titles_need_permission,
    windows_from_probe,
)
from meeting_notes.meet_detect import ChromeMeetDetector
from meeting_notes.platform_match import process_matches
from meeting_notes.slack_detect import SlackHuddleDetector
from meeting_notes.watch_guard import ENSURE_RETRY_DELAYS
from meeting_notes.autostart import AutostartError, autostart_status


PROBE = {
    "windows": [
        {"title": "Meet - abc-defg-hij - Google Chrome", "owner": "Google Chrome", "pid": 10},
        {"title": "#engineering - Acme - Slack", "owner": "Slack", "pid": 11},
        {"title": "", "owner": "Notes", "pid": 12},
    ],
    "inputs": [
        {
            "name": "Google Chrome Helper",
            "bundle_id": "com.google.Chrome",
            "pid": 13,
            "input_running": True,
        },
        {
            "name": "Slack",
            "bundle_id": "com.tinyspeck.slackmacgap",
            "pid": 14,
            "input_running": False,
        },
    ],
    "input_available": True,
}


def test_windows_process_match_is_unchanged():
    assert process_matches(r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome.exe", platform="win32")
    assert process_matches("chrome.exe", "chrome.exe", platform="win32")
    assert process_matches("slack.exe", "slack.exe", platform="win32")
    assert not process_matches("Google Chrome", "chrome.exe", platform="win32")
    assert not process_matches("Slack", "slack.exe", platform="win32")
    assert not process_matches("firefox.exe", "chrome.exe", platform="win32")


def test_darwin_aliases_match_chrome_and_slack_only_on_mac():
    assert process_matches("Google Chrome", "chrome.exe", platform="darwin")
    assert process_matches("Google Chrome Helper com.google.Chrome", "chrome.exe", platform="darwin")
    assert process_matches("Slack", "slack.exe", platform="darwin")
    assert process_matches("Slack Helper com.tinyspeck.slackmacgap", "slack.exe", platform="darwin")
    assert not process_matches("Google Chrome", "slack.exe", platform="darwin")
    assert not process_matches("Slack", "chrome.exe", platform="darwin")
    assert not process_matches("Google Chrome", "chrome.exe", platform="linux")


def test_meet_detector_accepts_a_mac_chrome_window_only_on_darwin(monkeypatch):
    import meeting_notes.platform_match as platform_match

    windows = windows_from_probe(PROBE)
    assert windows[0].process_name == "Google Chrome"

    monkeypatch.setattr(platform_match.sys, "platform", "darwin")
    found = ChromeMeetDetector(
        windows=lambda: windows,
        mic_in_use=lambda: True,
        process_marker="chrome.exe",
    ).poll()
    assert found is not None
    assert found.meet_code == "abc-defg-hij"

    monkeypatch.setattr(platform_match.sys, "platform", "win32")
    missed = ChromeMeetDetector(
        windows=lambda: windows,
        mic_in_use=lambda: True,
        process_marker="chrome.exe",
    ).poll()
    assert missed is None


def test_slack_detector_names_a_mac_window(monkeypatch):
    import meeting_notes.platform_match as platform_match

    monkeypatch.setattr(platform_match.sys, "platform", "darwin")
    detector = SlackHuddleDetector(
        windows=lambda: windows_from_probe(PROBE),
        mic_in_use=lambda _marker: True,
    )
    sighting = detector.poll()
    assert sighting is not None
    assert sighting.display_name == "#engineering"
    assert sighting.source == "slack-huddle"


def test_probe_json_becomes_a_coreaudio_mic_status():
    status = mic_status_from_probe(PROBE, "chrome.exe")
    assert status.in_use is True
    assert status.sources == ("coreaudio",)
    assert status.registry == ""
    assert status.wasapi == ""

    idle = mic_status_from_probe(PROBE, "slack.exe")
    assert idle.in_use is False

    missing = mic_status_from_probe({"error": "helper-missing"}, "chrome.exe")
    assert missing.in_use is False
    assert "setup_macos.sh" in missing.notice

    broken = parse_probe_json("not-json")
    assert broken["error"]
    assert titles_need_permission({"windows": [{"title": "", "owner": "Google Chrome"}]})
    assert not titles_need_permission({"windows": []})
    assert not titles_need_permission(PROBE)


def test_capture_backend_keeps_avfoundation_unless_the_helper_is_used():
    assert capture_backend_for({}, helper_exists=False) == "avfoundation"
    assert capture_backend_for({}, helper_exists=True) == "helper"
    assert capture_backend_for({"MEETING_NOTES_CAPTURE": "avfoundation"}, helper_exists=True) == "avfoundation"
    assert capture_backend_for({"MEETING_NOTES_CAPTURE": "blackhole"}, helper_exists=True) == "avfoundation"
    assert capture_backend_for({"MEETING_NOTES_CAPTURE": "helper"}, helper_exists=False) == "helper"
    argv = helper_record_argv("/tmp/meeting-audio-capture", "/tmp/audio.wav")
    assert argv == ["/tmp/meeting-audio-capture", "record", "--output", "/tmp/audio.wav"]


def test_launchd_plist_runs_supervise_and_has_no_windows_paths(tmp_path):
    control = tmp_path / "ctrl"
    control.mkdir()
    text = render_plist("/Users/me/.meeting-notes/venv/bin/python", str(control))
    assert "com.meeting-notes.watch" in text
    assert "<key>RunAtLoad</key>" in text
    assert "<key>SuccessfulExit</key>" in text
    assert "<string>Aqua</string>" in text
    assert "autostart" in text and "supervise" in text
    assert "/Users/me/.meeting-notes/venv/bin/python" in text
    assert "schtasks" not in text
    assert "pythonw" not in text
    assert "C:\\" not in text
    assert "MeetingNotesWatch" not in text
    assert str(control / "launchd.log") in text
    assert str(control / "bin") in text


def test_install_and_uninstall_launch_agent(tmp_path):
    control = tmp_path / "ctrl"
    agents = tmp_path / "LaunchAgents"
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if kwargs.get("check") and "bootstrap" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    meta = install_launch_agent(
        str(control),
        python="/Users/me/venv/bin/python",
        agents_dir=str(agents),
        run=run,
        uid=501,
    )
    assert meta["method"] == "launchd"
    assert meta["triggers"] == ["login", "wake", "unlock"]
    assert meta["retry_seconds"] == [30, 60, 120]
    assert ENSURE_RETRY_DELAYS == (30, 60, 120)
    plist = agents / "com.meeting-notes.watch.plist"
    assert plist.is_file()
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in calls
    assert ["launchctl", "kickstart", "-k", "gui/501/com.meeting-notes.watch"] in calls
    status = autostart_status(str(control))
    assert status["installed"] is True
    assert status["method"] == "launchd"
    assert "login" in status["detail"]

    removed = uninstall_launch_agent(str(control), agents_dir=str(agents), run=run, uid=501)
    assert str(plist) in removed
    assert not plist.exists()
    assert autostart_status(str(control))["installed"] is False


def test_launchd_bootstrap_failure_is_reported(tmp_path):
    control = tmp_path / "ctrl"
    agents = tmp_path / "LaunchAgents"

    def run(argv, **kwargs):
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(1, argv, stderr="Bootstrap failed: 5")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(AutostartError) as exc:
        install_launch_agent(
            str(control),
            python="/Users/me/venv/bin/python",
            agents_dir=str(agents),
            run=run,
            uid=501,
        )
    assert "Bootstrap failed" in str(exc.value)


def test_supervisor_ensures_on_login_and_wake_and_does_not_stop_a_recording():
    reasons = []

    def ensure(reason):
        reasons.append(reason)

        class _Result:
            user_message = "ensure: left the recording watcher alone"
            ok = True

        return _Result()

    events = iter(["wake", "unlock"])

    class _Log:
        def __init__(self):
            self.messages = []

        def info(self, message, *args):
            self.messages.append(message % args if args else message)

    seen = supervise(
        "/tmp/unused",
        ensure=ensure,
        wait_for_session=lambda: next(events),
        log=_Log(),
        max_events=2,
    )
    assert seen == 2
    assert reasons == ["launchd login", "launchd wake", "launchd unlock"]
    assert all("recording" not in reason for reason in reasons)


def test_wait_for_session_reads_the_helper_event():
    def run(argv, **kwargs):
        assert argv[-1] == "wait-session"
        return subprocess.CompletedProcess(argv, 0, "wake\n", "")

    assert wait_for_session_event(helper="/tmp/meeting-audio-capture", run=run, sleep=lambda _s: None) == "wake"

    def missing(_argv, **_kwargs):
        raise AssertionError("helper should not be launched")

    assert (
        wait_for_session_event(helper="", run=missing, sleep=lambda _s: None)
        == "helper-missing"
    )


def test_linux_watch_still_refuses(capsys):
    import meeting_notes.cli as cli

    if __import__("sys").platform in ("win32", "darwin"):
        return
    with pytest.raises(SystemExit) as exc:
        cli.cmd_watch(cli.build_parser().parse_args(["watch"]))
    assert exc.value.code == 1
    assert "Windows or macOS" in capsys.readouterr().err
