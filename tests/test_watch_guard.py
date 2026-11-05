import json
import logging
import logging.handlers
import os

import pytest

from meeting_notes.config import Config
from meeting_notes.deliver import DeliveryError
from meeting_notes.watch_guard import (
    HEARTBEAT_RECORDING_DEAD_SECONDS,
    HEARTBEAT_STALE_SECONDS,
    acquire_instance_lock,
    build_health_payload,
    consume_start_reason,
    decide,
    describe_heartbeat,
    ensure_watcher,
    maybe_send_health,
    read_heartbeat,
    write_instance,
    write_start_reason,
    Heartbeat,
)
from meeting_notes.watcher import configure_watch_logging


NOW = 1_700_000_000.0
PID = 4242


class ListLog:
    def __init__(self):
        self.messages = []

    def _fmt(self, msg, args):
        if not args:
            return str(msg)
        try:
            return msg % args
        except Exception:
            return f"{msg} {args}"

    def info(self, msg, *args):
        self.messages.append(("info", self._fmt(msg, args)))

    def warning(self, msg, *args):
        self.messages.append(("warning", self._fmt(msg, args)))

    def error(self, msg, *args):
        self.messages.append(("error", self._fmt(msg, args)))

    def exception(self, msg, *args):
        self.messages.append(("error", self._fmt(msg, args)))

    def text(self):
        return "\n".join(message for _level, message in self.messages)


def _heartbeat(age, state="idle", pid=PID):
    return {
        "unix": NOW - age,
        "pid": pid,
        "state": state,
        "timestamp": "2026-11-05T12:00:00-05:00",
        "version": "0.1.0",
    }


def _instance(age=0, pid=PID):
    return {"pid": pid, "started_at_unix": NOW - age, "version": "0.1.0"}


def _alive_factory(*living):
    living = {int(pid) for pid in living}

    def is_alive(pid):
        try:
            return int(pid) in living
        except (TypeError, ValueError):
            return False

    return is_alive


def test_only_one_process_can_hold_the_lock(tmp_path):
    control = str(tmp_path)
    first = acquire_instance_lock(control)
    assert first is not None
    assert acquire_instance_lock(control) is None
    first.release()
    second = acquire_instance_lock(control)
    assert second is not None
    second.release()


def test_heartbeat_records_state_pid_and_version(tmp_path):
    clock = {"now": NOW}

    def now():
        return clock["now"]

    beat = Heartbeat(
        str(tmp_path),
        interval=30,
        now=now,
        version="0.1.0",
        pid=PID,
        iso=lambda _unix: "2026-11-05T12:00:00-05:00",
    )
    assert beat.beat(state="idle") is True
    clock["now"] = NOW + 10
    assert beat.beat(state="recording") is False
    clock["now"] = NOW + 30
    assert beat.beat(state="recording") is True
    saved = read_heartbeat(str(tmp_path))
    assert saved["state"] == "recording"
    assert saved["pid"] == PID
    assert saved["version"] == "0.1.0"
    assert saved["unix"] == NOW + 30
    assert saved["timestamp"] == "2026-11-05T12:00:00-05:00"


def test_decide_thresholds():
    alive = _alive_factory(PID)
    dead = _alive_factory()

    fresh = decide(_instance(), _heartbeat(10, "idle"), NOW, alive, True)
    assert fresh.action == "healthy"
    assert fresh.kill is False

    just_under = decide(_instance(), _heartbeat(HEARTBEAT_STALE_SECONDS - 1), NOW, alive, True)
    assert just_under.action == "healthy"

    hung_idle = decide(_instance(), _heartbeat(HEARTBEAT_STALE_SECONDS, "idle"), NOW, alive, True)
    assert hung_idle.action == "restart"
    assert hung_idle.kill is True
    assert hung_idle.pid == PID

    protected = decide(
        _instance(),
        _heartbeat(HEARTBEAT_RECORDING_DEAD_SECONDS, "recording"),
        NOW,
        alive,
        True,
    )
    assert protected.action == "spare"
    assert protected.kill is False

    dead_recording = decide(
        _instance(),
        _heartbeat(HEARTBEAT_RECORDING_DEAD_SECONDS + 1, "recording"),
        NOW,
        alive,
        True,
    )
    assert dead_recording.action == "restart"
    assert dead_recording.kill is True

    starting = decide(_instance(age=10), None, NOW, alive, True)
    assert starting.action == "healthy"
    assert starting.kill is False

    no_beat = decide(_instance(age=HEARTBEAT_STALE_SECONDS), None, NOW, alive, True)
    assert no_beat.action == "restart"
    assert no_beat.kill is True

    absent = decide(None, None, NOW, dead, False)
    assert absent.action == "start"
    assert absent.kill is False
    assert "no watcher" in absent.reason

    crashed = decide(_instance(), _heartbeat(5), NOW, dead, False)
    assert crashed.action == "start"
    assert crashed.kill is False
    assert "not running" in crashed.reason

    reused = decide(_instance(), _heartbeat(400, "idle"), NOW, alive, False)
    assert reused.action == "start"
    assert reused.kill is False
    assert "not killing" in reused.reason


def _ensure(tmp_path, *, alive, lock_held, heartbeat=None, instance=None, kill=None, wait=None):
    control = str(tmp_path)
    if instance is not None:
        write_instance(control, instance["pid"], "0.1.0", instance["started_at_unix"])
    if heartbeat is not None:
        Heartbeat(
            control,
            now=lambda: heartbeat["unix"],
            version="0.1.0",
            pid=heartbeat["pid"],
            iso=lambda _unix: heartbeat["timestamp"],
        ).beat(force=True, state=heartbeat["state"])
    spawned = []
    killed = []

    def _kill(pid):
        killed.append(pid)
        if kill is not None:
            kill(pid)

    log = ListLog()
    decision = ensure_watcher(
        control,
        log=log,
        now=lambda: NOW,
        is_alive=alive,
        lock_held_fn=lambda: lock_held,
        kill=_kill,
        spawn=lambda reason: spawned.append(reason),
        wait_until_dead=wait or (lambda _pid: True),
    )
    return decision, spawned, killed, log


def test_ensure_leaves_a_healthy_watcher_alone(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=True,
        instance=_instance(),
        heartbeat=_heartbeat(12, "idle"),
    )
    assert decision.action == "healthy"
    assert decision.ok is True
    assert spawned == []
    assert killed == []
    assert "watcher already running" in log.text()


def test_ensure_starts_when_the_process_is_gone(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(),
        lock_held=False,
        instance=_instance(),
        heartbeat=_heartbeat(50, "idle"),
    )
    assert decision.action == "start"
    assert killed == []
    assert len(spawned) == 1
    assert "ensure start:" in spawned[0]
    assert "not running" in spawned[0]
    assert "starting watcher" in log.text()


def test_ensure_does_not_kill_a_reused_pid(tmp_path):
    decision, spawned, killed, _log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=False,
        instance=_instance(),
        heartbeat=_heartbeat(400, "idle"),
    )
    assert decision.action == "start"
    assert killed == []
    assert spawned
    assert "not killing" in spawned[0]


def test_ensure_kills_a_hung_idle_watcher_and_restarts(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=True,
        instance=_instance(),
        heartbeat=_heartbeat(200, "idle"),
    )
    assert decision.action == "restart"
    assert killed == [PID]
    assert "restart after hang-kill" in spawned[0]
    text = log.text()
    assert "hang-kill:" in text
    assert "restarting watcher" in text


def test_ensure_spares_a_recording_until_the_heartbeat_is_clearly_dead(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=True,
        instance=_instance(),
        heartbeat=_heartbeat(200, "recording"),
    )
    assert decision.action == "spare"
    assert spawned == []
    assert killed == []
    assert "not killing a recording watcher" in log.text()


def test_ensure_kills_a_recording_whose_heartbeat_is_past_ten_minutes(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=True,
        instance=_instance(),
        heartbeat=_heartbeat(HEARTBEAT_RECORDING_DEAD_SECONDS + 30, "recording"),
    )
    assert decision.action == "restart"
    assert killed == [PID]
    assert "recording" in log.text()
    assert spawned


def test_ensure_does_not_start_a_second_watcher_when_hang_kill_fails(tmp_path):
    decision, spawned, killed, log = _ensure(
        tmp_path,
        alive=_alive_factory(PID),
        lock_held=True,
        instance=_instance(),
        heartbeat=_heartbeat(200, "idle"),
        wait=lambda _pid: False,
    )
    assert decision.action == "failed"
    assert decision.ok is False
    assert killed == [PID]
    assert spawned == []
    assert "not starting a second watcher" in log.text()


def test_status_reports_a_stale_recording_heartbeat(tmp_path):
    Heartbeat(
        str(tmp_path),
        now=lambda: NOW - 200,
        version="0.1.0",
        pid=PID,
        iso=lambda _unix: "2026-11-05T12:00:00-05:00",
    ).beat(force=True, state="recording")
    text = describe_heartbeat(str(tmp_path), now=NOW)
    assert "state=recording" in text
    assert "age 200s" in text
    assert "left alone" in text
    assert "version=0.1.0" in text


def test_start_reason_is_consumed_once_and_ignored_when_old(tmp_path):
    write_start_reason(str(tmp_path), "ensure start: no watcher is running")
    assert consume_start_reason(str(tmp_path), environ={}, now=None).startswith("ensure start:")
    assert consume_start_reason(str(tmp_path), environ={}, now=None) == "manual start"

    path = tmp_path / "watch-start-reason.json"
    path.write_text(
        json.dumps({"reason": "restart after hang-kill", "unix": 1}),
        encoding="utf-8",
    )
    assert consume_start_reason(str(tmp_path), environ={}, now=10_000) == "manual start"
    assert not path.exists()
    assert (
        consume_start_reason(str(tmp_path), environ={"MEETING_NOTES_WATCH_START_REASON": "from-env"})
        == "from-env"
    )


def test_watch_log_records_tracebacks_and_rotates(tmp_path):
    logger, path = configure_watch_logging(str(tmp_path), max_bytes=1_000_000, backup_count=3)
    file_handler = next(
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    )
    assert file_handler.maxBytes == 1_000_000
    assert file_handler.backupCount == 3
    logger.info("Watcher starting: pid=1 version=0.1.0 reason=manual start")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Unhandled exception in watcher")
    logger.warning("hang-kill: pid=1 state=idle heartbeat_age=200s reason=stale")
    logger.info("Watcher stopped: pid=1 reason=stop requested")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "Watcher starting:" in text
    assert "Unhandled exception in watcher" in text
    assert "RuntimeError: boom" in text
    assert "hang-kill:" in text
    assert "Watcher stopped:" in text

    rotating, rotated_path = configure_watch_logging(
        str(tmp_path / "rotated"),
        max_bytes=300,
        backup_count=2,
    )
    for index in range(20):
        rotating.warning("ensure: starting watcher (line %s is long enough to roll the file)", index)
    names = os.listdir(tmp_path / "rotated")
    assert os.path.basename(rotated_path) == "watch.log"
    assert any(name.startswith("watch.log.") for name in names)


def test_health_payload_is_not_a_transcript():
    payload = build_health_payload(
        1200,
        {"state": "idle", "timestamp": "2026-11-05T12:00:00-05:00"},
        pid=7,
        version="0.1.0",
        started_at="2026-11-05T12:20:00-05:00",
    )
    assert payload["type"] == "watcher-health"
    assert payload["down_seconds"] == 1200
    assert "transcript" not in payload
    assert "webhook" not in payload


def test_health_ping_sends_only_for_a_long_same_day_gap(tmp_path):
    secret = "super-secret-token"
    config = Config(
        webhook_url="https://example.invalid/meeting-notes",
        webhook_auth_header="Authorization",
        webhook_auth_value=secret,
        watcher_health_ping=True,
        delivery_attempts=2,
        delivery_backoff_seconds=1,
        delivery_timeout_seconds=5,
    )
    captured = []

    def poster(url, payload, header_name, header_value, timeout):
        captured.append(payload)
        assert header_value == secret
        assert url == config.webhook_url
        return 200

    previous = _heartbeat(20 * 60, "idle")
    log = ListLog()
    sent = maybe_send_health(
        config,
        previous,
        NOW,
        log=log,
        poster=poster,
        sleep=lambda _seconds: None,
        same_local_day=lambda _previous, _now: True,
        pid=PID,
        version="0.1.0",
        started_at="2026-11-05T12:20:00-05:00",
    )
    assert sent is True
    assert captured[0]["type"] == "watcher-health"
    assert captured[0]["down_seconds"] == 1200
    assert "transcript" not in captured[0]
    assert secret not in log.text()
    assert not (tmp_path / "outbox").exists()

    captured.clear()
    assert (
        maybe_send_health(
            config,
            _heartbeat(5 * 60),
            NOW,
            poster=poster,
            sleep=lambda _seconds: None,
            same_local_day=lambda _a, _b: True,
        )
        is False
    )
    assert captured == []

    assert (
        maybe_send_health(
            config,
            previous,
            NOW,
            poster=poster,
            sleep=lambda _seconds: None,
            same_local_day=lambda _a, _b: False,
            log=log,
        )
        is False
    )
    assert "local midnight" in log.text()
    assert captured == []

    off = Config(webhook_url=config.webhook_url, watcher_health_ping=False)
    assert maybe_send_health(off, previous, NOW, poster=poster, same_local_day=lambda _a, _b: True) is False

    no_url = Config(webhook_url="", watcher_health_ping=True)
    assert (
        maybe_send_health(
            no_url,
            previous,
            NOW,
            poster=poster,
            same_local_day=lambda _a, _b: True,
            log=log,
        )
        is False
    )
    assert captured == []


def test_health_ping_redacts_and_does_not_write_outbox(tmp_path, monkeypatch):
    secret = "super-secret-token"
    config = Config(
        webhook_url="https://example.invalid/meeting-notes",
        webhook_auth_value=secret,
        watcher_health_ping=True,
    )
    outbox = tmp_path / "outbox"
    monkeypatch.chdir(tmp_path)

    def poster(_url, _payload, _header_name, _header_value, _timeout):
        raise DeliveryError(f"HTTP 500 {secret}")

    log = ListLog()
    sent = maybe_send_health(
        config,
        _heartbeat(20 * 60),
        NOW,
        log=log,
        poster=poster,
        sleep=lambda _seconds: None,
        same_local_day=lambda _a, _b: True,
    )
    assert sent is False
    assert secret not in log.text()
    assert "[redacted]" in log.text()
    assert "watcher-health ping failed" in log.text()
    assert not outbox.exists()


def test_default_config_leaves_the_health_ping_off(tmp_path):
    from meeting_notes.config import load_config

    config = load_config(control_dir=str(tmp_path), environ={})
    assert config.watcher_health_ping is False
    path = tmp_path / "on.toml"
    path.write_text("watcher_health_ping = true\n", encoding="utf-8")
    assert load_config(path=str(path), environ={}).watcher_health_ping is True
