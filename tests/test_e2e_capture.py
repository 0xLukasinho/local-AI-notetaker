"""End to end on a real Mac: three voices through the real capture path.

Run with:  MEETING_NOTES_E2E=1 python -m pytest tests/test_e2e_capture.py -s

It plays a clip of three distinct macOS voices (self-introductions, people
addressing each other) through the speakers and records it with
`recorder_mac.record_audio(..., separate_tracks=True)` - the same call the
watcher makes - then transcribes and names the speakers. Skipped elsewhere
and while a call is being recorded. Never touches the voice-profile store,
the calendar, the LaunchAgent, or any session folder.
"""

import io
import os
import sys
import tempfile

import pytest

from meeting_notes import selftest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("MEETING_NOTES_E2E") != "1",
    reason="real-capture e2e: macOS only, set MEETING_NOTES_E2E=1",
)


@pytest.fixture
def real_home(monkeypatch):
    # conftest points HOME at a temp dir; the capture helper and models live
    # in the real one. Read-only use: nothing under it is written.
    import pwd

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)


def test_three_voices_through_the_capture_path_are_recorded_and_named(real_home, tmp_path):
    import subprocess as sp

    if selftest.recording_now():
        pytest.skip("a call is being recorded")
    from meeting_notes.config import load_config

    config = load_config()
    clip, turns = selftest.build_clip(str(tmp_path))
    folder = tmp_path / "session"
    folder.mkdir()
    result = selftest.capture_clip(str(folder), clip)
    out = io.StringIO()
    report = selftest.Report(out)
    assert selftest.judge_capture(result, report), out.getvalue()
    others = result["tracks"]["audio-others.wav"]
    assert others["rms"] >= 20 and others["nonzero"] >= 0.05  # non-silent remote track
    assert others["ratio"] >= 0.95  # length >= 95% of wall clock
    text, document = selftest.name_capture(str(folder), config, model=os.environ.get("MEETING_NOTES_E2E_MODEL", "small"))
    print(out.getvalue())
    print(text)
    assert selftest.judge_names(document, report), out.getvalue()
    # Every remote line carries a person's name, not "Speaker N".
    remote = [s for s in document["segments"] if s.get("track") != "self"]
    assert remote and all(not s["speaker"].startswith("Speaker") for s in remote)
