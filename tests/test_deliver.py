import json
import os

import pytest

from meeting_notes.config import Config
from meeting_notes.deliver import (
    DeliveryError,
    build_payload,
    deliver_folder,
    post_with_retry,
    retry_outbox,
    urllib_poster,
)
from meeting_notes.session import read_status, write_status


SECRET = "super-secret-token"


def _config(**kwargs):
    base = dict(
        webhook_url="https://example.invalid/hooks/meetings",
        webhook_auth_header="X-Webhook-Token",
        webhook_auth_value=SECRET,
        delivery_attempts=3,
        delivery_backoff_seconds=1,
        delivery_timeout_seconds=5,
    )
    base.update(kwargs)
    return Config(**base)


def _session(tmp_path, transcript="[00:01] hello\n[00:02] world"):
    folder = tmp_path / "2026-10-29_abc-defg-hij_1430"
    folder.mkdir()
    (folder / "transcript.txt").write_text(transcript, encoding="utf-8")
    (folder / "audio.wav").write_bytes(b"RIFFaudio")
    write_status(
        str(folder),
        {
            "state": "done",
            "meet_code": "abc-defg-hij",
            "window_title": "Meet - abc-defg-hij - Google Chrome",
            "joined_at": "2026-10-29T14:00:00+00:00",
            "left_at": "2026-10-29T14:30:00+00:00",
            "whisper_model": "medium",
            "language": "en",
            "delivered": False,
        },
    )
    return folder


def test_payload_fields_and_duration(tmp_path):
    folder = _session(tmp_path)
    payload = build_payload(read_status(str(folder)), "[00:01] hello\n[00:02] world", str(folder))
    assert payload["folder_name"] == folder.name
    assert payload["folder_path"] == os.path.abspath(str(folder))
    assert payload["meet_code"] == "abc-defg-hij"
    assert payload["window_title"] == "Meet - abc-defg-hij - Google Chrome"
    assert payload["joined_at"] == "2026-10-29T14:00:00+00:00"
    assert payload["left_at"] == "2026-10-29T14:30:00+00:00"
    assert payload["duration_seconds"] == 1800
    assert payload["whisper_model"] == "medium"
    assert payload["language"] == "en"
    assert payload["transcript"] == "[00:01] hello\n[00:02] world"


def test_post_retries_with_backoff_then_succeeds():
    calls = []
    sleeps = []

    def poster(url, payload, header, value, timeout):
        calls.append((url, header, value, payload["meet_code"]))
        if len(calls) < 3:
            raise DeliveryError("HTTP 500")
        return 200

    ok, error = post_with_retry(_config(), {"meet_code": "abc-defg-hij"}, poster, sleeps.append)
    assert ok is True
    assert error is None
    assert sleeps == [1.0, 2.0]
    assert len(calls) == 3
    assert calls[0][1:] == ("X-Webhook-Token", SECRET, "abc-defg-hij")


def test_failed_delivery_is_written_to_the_outbox_without_the_secret(tmp_path):
    folder = _session(tmp_path)
    outbox = tmp_path / "outbox"

    def poster(url, payload, header, value, timeout):
        assert value == SECRET
        raise DeliveryError("HTTP 503")

    ok = deliver_folder(
        str(folder),
        _config(),
        poster=poster,
        sleep=lambda _seconds: None,
        outbox=str(outbox),
    )
    assert ok is False
    entry_path = outbox / f"{folder.name}.json"
    text = entry_path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "HTTP 503" in text
    body = json.loads(text)
    assert body["payload"]["transcript"].startswith("[00:01] hello")
    assert read_status(str(folder))["delivered"] is False
    assert (folder / "audio.wav").is_file()


def test_successful_delivery_marks_status_and_can_delete_audio(tmp_path):
    folder = _session(tmp_path)

    def poster(url, payload, header, value, timeout):
        return 204

    ok = deliver_folder(
        str(folder),
        _config(delete_audio_after_delivery=True),
        poster=poster,
        sleep=lambda _seconds: None,
        outbox=str(tmp_path / "outbox"),
    )
    assert ok is True
    status = read_status(str(folder))
    assert status["delivered"] is True
    assert status["delivered_at"]
    assert not (folder / "audio.wav").exists()


def test_delete_audio_stays_off_by_default(tmp_path):
    folder = _session(tmp_path)

    def poster(*_args, **_kwargs):
        return 200

    assert deliver_folder(str(folder), _config(), poster=poster, sleep=lambda _s: None) is True
    assert (folder / "audio.wav").is_file()


def test_missing_webhook_does_not_create_an_outbox_entry(tmp_path):
    folder = _session(tmp_path)
    outbox = tmp_path / "outbox"
    ok = deliver_folder(str(folder), _config(webhook_url=""), outbox=str(outbox))
    assert ok is False
    assert not outbox.exists()


def test_outbox_retry_posts_and_removes_the_file(tmp_path):
    folder = _session(tmp_path)
    outbox = tmp_path / "outbox"
    seen = []

    def fail(url, payload, header, value, timeout):
        raise DeliveryError("HTTP 500")

    deliver_folder(str(folder), _config(), poster=fail, sleep=lambda _s: None, outbox=str(outbox))

    def succeed(url, payload, header, value, timeout):
        seen.append(payload["folder_name"])
        assert value == SECRET
        return 200

    count = retry_outbox(str(outbox), _config(delete_audio_after_delivery=True), poster=succeed, sleep=lambda _s: None)
    assert count == 1
    assert seen == [folder.name]
    assert list(outbox.glob("*.json")) == []
    assert read_status(str(folder))["delivered"] is True
    assert not (folder / "audio.wav").exists()
    assert SECRET not in json.dumps(seen)


def test_urllib_poster_sends_json_and_auth_header(monkeypatch):
    captured = {}

    class _Response:
        status = 204

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=30):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        return _Response()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status = urllib_poster(
        "https://example.invalid/hook",
        {"transcript": "[00:00] hi"},
        "X-Webhook-Token",
        SECRET,
        12,
    )
    assert status == 204
    assert captured["url"] == "https://example.invalid/hook"
    assert captured["timeout"] == 12
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["x-webhook-token"] == SECRET
    assert json.loads(captured["data"].decode("utf-8"))["transcript"] == "[00:00] hi"


def test_cmd_deliver_resends(tmp_path, monkeypatch, capsys):
    import meeting_notes.cli as cli
    import meeting_notes.deliver as deliver

    folder = _session(tmp_path)
    monkeypatch.setattr(cli, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path / "control"))

    def poster(url, payload, header, value, timeout):
        return 200

    monkeypatch.setattr(deliver, "urllib_poster", poster)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda control_dir=None: _config(),
    )
    cli.cmd_deliver(cli.build_parser().parse_args(["deliver", folder.name]))
    assert "Delivered" in capsys.readouterr().out
    assert read_status(str(folder))["delivered"] is True


def test_cmd_deliver_requires_a_webhook(tmp_path, monkeypatch, capsys):
    import meeting_notes.cli as cli

    folder = _session(tmp_path)
    monkeypatch.setattr(cli, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(cli, "load_config", lambda control_dir=None: _config(webhook_url=""))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_deliver(cli.build_parser().parse_args(["deliver", folder.name]))
    assert exc.value.code == 1
    assert "webhook_url is not configured" in capsys.readouterr().err
