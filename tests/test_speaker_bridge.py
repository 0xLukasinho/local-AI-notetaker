"""The speaker bridge and the one-time extension copy. No Chrome, no audio."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from meeting_notes.extension_install import extension_source_dir, install_extension
from meeting_notes.speaker_bridge import SpeakerBridge
from meeting_notes.speakers import load_speaker_events


class _Clock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        if len(self.values) == 1:
            return self.values[0]
        return self.values.pop(0)


def test_bridge_stamps_server_time_and_rejects_bad_bodies(tmp_path):
    path = tmp_path / "speakers.jsonl"
    clock = _Clock([1000.0, 1003.25])
    bridge = SpeakerBridge(str(path), port=0, clock=clock)
    bridge.start()
    try:
        bridge.mark_audio_start()
        def post(body):
            data = json.dumps(body).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{bridge.bound_port}/speakers",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()

        status, raw = post(
            {
                "self": "Alex Chen",
                "active": [{"id": "dev-ada", "name": "Ada Lovelace", "self": False}],
                "participants": [
                    {"id": "dev-self", "name": "Alex Chen", "self": True},
                    {"id": "dev-ada", "name": "Ada Lovelace", "self": False},
                ],
            }
        )
        assert status == 200
        assert json.loads(raw)["ok"] is True

        bad = urllib.request.Request(
            f"http://127.0.0.1:{bridge.bound_port}/speakers",
            data=b"not-json",
            method="POST",
        )
        try:
            urllib.request.urlopen(bad, timeout=2)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = True
            assert exc.code == 400
        assert raised is True

        health = urllib.request.urlopen(
            f"http://127.0.0.1:{bridge.bound_port}/health", timeout=2
        )
        assert json.loads(health.read())["ok"] is True
    finally:
        bridge.stop()

    events = load_speaker_events(str(path))
    assert len(events) == 1
    assert events[0]["t"] == 3.25
    assert events[0]["active"] == ["Ada Lovelace"]
    assert events[0]["self"] == "Alex Chen"
    assert events[0]["named_ids"]["dev-ada"] == "Ada Lovelace"
    assert "Ada Lovelace" in events[0]["participants"]


def test_samples_queued_before_audio_start_share_that_clock(tmp_path):
    path = tmp_path / "speakers.jsonl"
    clock = _Clock([1.0, 2.5])
    bridge = SpeakerBridge(str(path), port=0, clock=clock)
    bridge.start()
    try:
        data = json.dumps({"active": ["Ada Lovelace"], "participants": ["Ada Lovelace"]}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge.bound_port}/speakers",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
        bridge.mark_audio_start()
    finally:
        bridge.stop()
    events = load_speaker_events(str(path))
    assert len(events) == 1
    assert events[0]["t"] == -1.5
    assert events[0]["active"] == ["Ada Lovelace"]


def test_bridge_does_not_warn_for_a_solo_call_or_when_captions_are_on(tmp_path, capsys):
    path = tmp_path / "speakers.jsonl"
    bridge = SpeakerBridge(str(path), port=0, clock=lambda: 10.0)
    bridge.mark_audio_start()
    solo = {
        "self": "Alex Chen",
        "participants": [
            {"id": "spaces/x/devices/537", "name": "Alex Chen", "self": True},
            {
                "id": "spaces/x/devices/537",
                "name": "Others might still see your full video.",
                "self": False,
            },
        ],
    }
    for _index in range(8):
        bridge.record(solo)
    assert "Press CC" not in capsys.readouterr().out

    remote_with_captions_on = {
        "self": "Alex Chen",
        "captions_enabled": True,
        "participants": [
            {"id": "self", "name": "Alex Chen", "self": True},
            {"id": "ada", "name": "Ada Lovelace", "self": False},
        ],
    }
    for _index in range(8):
        bridge.record(remote_with_captions_on)
    assert "Press CC" not in capsys.readouterr().out


def test_bridge_warns_once_when_captions_stay_off(tmp_path, capsys):
    path = tmp_path / "speakers.jsonl"
    bridge = SpeakerBridge(str(path), port=0, clock=lambda: 10.0)
    bridge.mark_audio_start()
    for _index in range(7):
        bridge.record({"active": ["Ada Lovelace"], "participants": ["Ada Lovelace"]})
    assert "Press CC" not in capsys.readouterr().out
    bridge.record({"active": ["Ada Lovelace"], "participants": ["Ada Lovelace"]})
    assert "Press CC" in capsys.readouterr().out
    bridge.record({"caption_speaker": "Ada Lovelace", "active": ["Ada Lovelace"]})
    assert capsys.readouterr().out == ""
    events = load_speaker_events(str(path))
    assert events[0]["caption_speaker"] == ""
    assert events[-1]["caption_speaker"] == "Ada Lovelace"


def test_speakers_command_prints_the_one_time_chrome_steps(tmp_path, monkeypatch, capsys):
    import meeting_notes.cli as cli

    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    cli.cmd_speakers(cli.build_parser().parse_args(["speakers"]))
    out = capsys.readouterr().out
    assert "chrome://extensions" in out
    assert "Load unpacked" in out
    assert "Reload" in out
    assert "/presence" in out
    assert (tmp_path / "chrome-extension" / "manifest.json").is_file()
    assert (tmp_path / "chrome-extension" / "content.js").is_file()


def test_extension_install_copies_files_and_writes_the_port(tmp_path):
    source = extension_source_dir()
    with open(os.path.join(source, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.loads(handle.read())
    assert manifest["manifest_version"] == 3
    assert any(item["js"] == ["content.js"] for item in manifest["content_scripts"])
    with open(os.path.join(source, "background.js"), encoding="utf-8") as handle:
        background = handle.read()
    assert "const BRIDGE_PORT = 8765;" in background
    assert "BRIDGE_PORT + 1" in background
    assert "/presence" in background
    with open(os.path.join(source, "content.js"), encoding="utf-8") as handle:
        content = handle.read()
    assert "data-participant-id" in content
    assert "data-self-name" in content
    assert "innerHTML" not in content

    folder = install_extension(str(tmp_path), 9876)
    with open(os.path.join(folder, "background.js"), encoding="utf-8") as handle:
        copied = handle.read()
    assert "const BRIDGE_PORT = 9876;" in copied
    assert "const BRIDGE_PORT = 8765;" not in copied
    content = (Path(folder) / "content.js").read_text(encoding="utf-8")
    assert "var PRESENCE_PORT = 9877;" in content
    assert "var BRIDGE_PORT = 9876;" in content
    assert "var PRESENCE_PORT = 8766;" not in content
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as handle:
        assert json.loads(handle.read())["name"]


def test_chrome_unpacked_extension_id_is_deterministic(tmp_path):
    from meeting_notes.extension_install import chrome_unpacked_extension_id

    folder = tmp_path / "chrome-extension"
    folder.mkdir()
    ext_id = chrome_unpacked_extension_id(str(folder))
    assert len(ext_id) == 32
    assert set(ext_id) <= set("abcdefghijklmnop")
    assert chrome_unpacked_extension_id(str(folder)) == ext_id


def test_inspect_extension_state_detects_chrome_path_mismatch(tmp_path, capsys, monkeypatch):
    import meeting_notes.cli as cli
    from meeting_notes.extension_install import chrome_unpacked_extension_id, inspect_extension_state

    control = tmp_path / "control"
    install_extension(str(control), 8765)

    other = tmp_path / "other-ext"
    other.mkdir()
    (other / "manifest.json").write_text('{"name":"Meeting Notes Speaker Tags","version":"1.3.0"}', encoding="utf-8")
    chrome_dir = tmp_path / "Chrome"
    default_prof = chrome_dir / "Default"
    default_prof.mkdir(parents=True)
    other_id = chrome_unpacked_extension_id(str(other))
    (default_prof / "Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        other_id: {
                            "path": str(other),
                            "manifest": {"name": "Meeting Notes Speaker Tags", "version": "1.3.0"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state = inspect_extension_state(str(control), chrome_dir=str(chrome_dir))
    assert state["chrome_accessible"] is True
    assert state["installed_version"] == "1.5.0"
    assert len(state["mismatches"]) == 1
    assert state["mismatches"][0]["path_differs"] is True
    assert state["mismatches"][0]["id"] == other_id

    # update_loaded=True updates non-git loaded copies in place to keep the Chrome ID.
    install_extension(str(control), 8765, update_loaded=True, chrome_dir=str(chrome_dir))
    assert json.loads((other / "manifest.json").read_text(encoding="utf-8"))["version"] == "1.5.0"

    monkeypatch.setattr(cli, "CONTROL_DIR", str(control))
    monkeypatch.setattr("meeting_notes.extension_install._default_chrome_dir", lambda: str(chrome_dir))
    cli.cmd_extension_status(cli.build_parser().parse_args(["extension", "status"]))
    out = capsys.readouterr().out
    assert "Mismatch:" in out
    assert other_id in out
