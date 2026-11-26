"""Signed-helper shim retirement, log labels, and Meet-extension naming."""

import json
import os
from pathlib import Path

from meeting_notes.macos_capture import retire_shim, signed_helper_path
from meeting_notes.platform_match import display_label
from meeting_notes.slack_detect import DESKTOP_MARKER, _format_mic
from meeting_notes.meet_detect import MicStatus
from meeting_notes.speakers import apply_naming


def test_display_label_uses_app_names_on_macos_not_windows_exe():
    assert display_label("slack.exe", platform="darwin") == "Slack"
    assert display_label("chrome.exe", platform="darwin") == "Google Chrome"
    assert display_label("slack.exe", platform="win32") == "slack.exe"
    status = MicStatus(False, ())
    line = _format_mic(status)
    # The function uses the current platform. On macOS this must not say slack.exe.
    if os.sys.platform == "darwin":
        assert "slack.exe" not in line
        assert "Slack" in line
    else:
        assert "slack.exe" in line


def test_retire_shim_moves_the_library_and_points_the_symlink(tmp_path, monkeypatch):
    home = tmp_path / "home"
    bindir = home / ".meeting-notes" / "bin"
    shim = home / ".meeting-notes" / "shim"
    signed_app = bindir / "MeetingAudioCapture-signed.app" / "Contents" / "MacOS"
    signed_app.mkdir(parents=True)
    helper = signed_app / "meeting-audio-capture"
    helper.write_text("x", encoding="utf-8")
    shim.mkdir(parents=True)
    (shim / "libtapfix.dylib").write_text("shim", encoding="utf-8")
    legacy = bindir / "MeetingAudioCapture.app" / "Contents" / "MacOS"
    legacy.mkdir(parents=True)
    (legacy / "meeting-audio-capture").write_text("old", encoding="utf-8")
    link = bindir / "meeting-audio-capture"
    link.symlink_to(legacy / "meeting-audio-capture")
    moved = retire_shim(home=str(home))
    assert moved and Path(moved).exists()
    assert not shim.exists()
    assert (Path(moved) / "libtapfix.dylib").is_file()
    assert link.is_symlink()
    assert Path(os.path.realpath(link)) == Path(os.path.realpath(helper))
    # Second call is a no-op besides keeping the symlink.
    assert retire_shim(home=str(home)) is None


def test_apply_naming_uses_meet_participants_from_status(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    (folder / "status.json").write_text(
        json.dumps(
            {
                "window_title": "Meet - abc-defg-hij",
                "meet_code": "abc-defg-hij",
                "meet_participants": ["Alex Rivera", "Jordan Blake"],
            }
        ),
        encoding="utf-8",
    )
    turns = [
        {"start": 0.0, "end": 2.0, "speaker": "Lukas", "text": "Hi both, thanks for joining."},
        {"start": 2.0, "end": 4.0, "speaker": "Speaker 2", "text": "Hi, this is Alex Rivera."},
        {"start": 4.0, "end": 6.0, "speaker": "Speaker 3", "text": "And I'm Jordan."},
    ]
    renamed, report = apply_naming(
        str(folder),
        turns,
        voice_segments=[],
        voice_embeddings={},
        events=[],
        self_label="Lukas",
        naming={"status": json.loads((folder / "status.json").read_text()), "update_profiles": False, "profiles_path": False},
    )
    names = [turn["speaker"] for turn in renamed]
    assert "Alex Rivera" in names
    assert "Jordan" in names or "Jordan Blake" in names
