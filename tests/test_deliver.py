import hashlib
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


def _long_transcript():
    line = '[00:01] she said "hello" and the remote side answered\n'
    return line * 80


def test_long_transcript_is_posted_in_sized_chunks_with_the_auth_header(tmp_path):
    transcript = _long_transcript()
    folder = _session(tmp_path, transcript)
    secret = "Bearer " + SECRET
    seen = []
    logged = []

    class _Log:
        def info(self, message, *args):
            logged.append(message % args if args else message)

        def error(self, message, *args):
            logged.append(message % args if args else message)

    def poster(url, payload, header, value, timeout):
        seen.append((header, value, payload))
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        assert len(raw) <= 2500
        assert secret not in raw.decode("utf-8")
        return 200

    ok = deliver_folder(
        str(folder),
        _config(
            webhook_auth_header="Authorization",
            webhook_auth_value=secret,
            webhook_max_body_bytes=2500,
        ),
        poster=poster,
        sleep=lambda _seconds: None,
        log=_Log(),
    )
    assert ok is True
    assert len(seen) > 1
    assert "".join(item[2]["transcript"] for item in seen) == transcript
    indexes = [item[2]["chunk_index"] for item in seen]
    assert indexes == list(range(len(seen)))
    assert {item[2]["chunk_count"] for item in seen} == {len(seen)}
    assert {item[2]["transcript_chars"] for item in seen} == {len(transcript)}
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    assert all(item[2]["transcript_sha256"] == digest for item in seen)
    assert all(item[0] == "Authorization" and item[1] == secret for item in seen)
    # Short lines stay whole. Every piece but a hard-split ends on a newline.
    for item in seen[:-1]:
        assert item[2]["transcript"].endswith("\n")
    assert secret not in "\n".join(logged)
    assert f"{len(transcript)} chars" in logged[0]
    assert f"{len(seen)} request" in logged[0]


def test_failed_later_chunk_keeps_the_full_transcript_and_hides_the_key(tmp_path):
    transcript = _long_transcript()
    folder = _session(tmp_path, transcript)
    outbox = tmp_path / "outbox"
    logged = []
    calls = {"n": 0}

    class _Log:
        def info(self, message, *args):
            logged.append(message % args if args else message)

        def error(self, message, *args):
            logged.append(message % args if args else message)

    def poster(url, payload, header, value, timeout):
        calls["n"] += 1
        if payload["chunk_index"] == 0:
            return 200
        raise DeliveryError(f"HTTP 500 token={value}")

    ok = deliver_folder(
        str(folder),
        _config(webhook_max_body_bytes=2500, delivery_attempts=1),
        poster=poster,
        sleep=lambda _seconds: None,
        outbox=str(outbox),
        log=_Log(),
    )
    assert ok is False
    text = (outbox / f"{folder.name}.json").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "[redacted]" in text
    body = json.loads(text)
    assert body["payload"]["transcript"] == transcript
    assert "chunk_index" not in body["payload"]
    assert SECRET not in "\n".join(logged)
    status = read_status(str(folder))
    assert SECRET not in (status.get("delivery_error") or "")
    assert "chunk 2/" in status["delivery_error"]

    resent = []

    def succeed(url, payload, header, value, timeout):
        resent.append(payload["chunk_index"])
        assert value == SECRET
        return 200

    count = retry_outbox(
        str(outbox),
        _config(webhook_max_body_bytes=2500, delivery_attempts=1),
        poster=succeed,
        sleep=lambda _s: None,
    )
    assert count == 1
    assert resent == list(range(len(resent)))
    assert len(resent) > 1
    assert list(outbox.glob("*.json")) == []


def test_overlong_line_is_split_and_rejoins():
    from meeting_notes.deliver import split_transcript_to_fit

    text = ("a" * 50) + "\n" + ("b" * 10) + "\n"
    parts = split_transcript_to_fit(text, lambda piece: len(piece) <= 30)
    assert "".join(parts) == text
    assert parts
    assert all(len(part) <= 30 for part in parts)


def test_zero_body_limit_sends_one_request(tmp_path):
    transcript = _long_transcript()
    folder = _session(tmp_path, transcript)
    seen = []

    def poster(url, payload, header, value, timeout):
        seen.append(payload)
        return 200

    assert deliver_folder(
        str(folder),
        _config(webhook_max_body_bytes=0),
        poster=poster,
        sleep=lambda _seconds: None,
    )
    assert len(seen) == 1
    assert seen[0]["transcript"] == transcript
    assert seen[0]["chunk_count"] == 1
    assert seen[0]["chunk_index"] == 0


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
