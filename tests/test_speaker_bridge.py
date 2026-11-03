"""The speaker bridge and the one-time extension copy. No Chrome, no audio."""

import json
import os
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


def test_speakers_command_prints_the_one_time_chrome_steps(tmp_path, monkeypatch, capsys):
    import meeting_notes.cli as cli

    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    cli.cmd_speakers(cli.build_parser().parse_args(["speakers"]))
    out = capsys.readouterr().out
    assert "chrome://extensions" in out
    assert "Load unpacked" in out
    assert "Them" in out
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
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as handle:
        assert json.loads(handle.read())["name"]
