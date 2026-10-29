import json
import os

import pytest

from meeting_notes.config import ConfigError, _parse_simple_toml, load_config


def test_missing_file_keeps_manual_summary_and_watcher_transcript_only(tmp_path):
    config = load_config(control_dir=str(tmp_path), environ={})
    assert config.transcript_only is False
    assert config.watcher_transcript_only is True
    assert config.delete_audio_after_delivery is False
    assert config.webhook_url == ""
    assert config.join_debounce_seconds == 12
    assert config.leave_debounce_seconds == 25
    assert config.mic_process_marker == "chrome.exe"


def test_toml_and_env_override(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '\n'.join(
            [
                'webhook_url = "https://example.invalid/from-file"',
                'webhook_auth_header = "X-Token"',
                'webhook_auth_value = "file-secret"',
                "join_debounce_seconds = 10",
                "leave_debounce_seconds = 30",
                "transcript_only = false",
                "watcher_transcript_only = true",
                "delete_audio_after_delivery = true",
                'window_title_regex = ""',
                'mic_process_marker = "chrome.exe"',
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(
        control_dir=str(tmp_path),
        environ={"MEETING_NOTES_WEBHOOK_URL": "https://example.invalid/from-env"},
    )
    assert config.webhook_url == "https://example.invalid/from-env"
    assert config.webhook_auth_value == "file-secret"
    assert config.join_debounce_seconds == 10
    assert config.leave_debounce_seconds == 30
    assert config.delete_audio_after_delivery is True


def test_json_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"webhook_url": "https://example.invalid/json", "transcript_only": True}),
        encoding="utf-8",
    )
    config = load_config(control_dir=str(tmp_path), environ={})
    assert config.webhook_url == "https://example.invalid/json"
    assert config.transcript_only is True


def test_simple_toml_parser_and_invalid_file(tmp_path):
    parsed = _parse_simple_toml(
        '# comment\nwebhook_url = "https://example.invalid/hook" # inline\n'
        'webhook_auth_value = "secret"\njoin_debounce_seconds = 15\n'
        "delete_audio_after_delivery = false\n"
    )
    assert parsed["webhook_url"] == "https://example.invalid/hook"
    assert parsed["webhook_auth_value"] == "secret"
    assert parsed["join_debounce_seconds"] == 15
    assert parsed["delete_audio_after_delivery"] is False

    bad = tmp_path / "config.toml"
    bad.write_text("webhook_url = [\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=str(bad), environ={})


def test_example_config_loads(tmp_path):
    example_path = os.path.join(os.path.dirname(__file__), "..", "config.example.toml")
    example = open(example_path, encoding="utf-8").read()
    target = tmp_path / "config.toml"
    target.write_text(example, encoding="utf-8")
    config = load_config(control_dir=str(tmp_path), environ={})
    assert config.webhook_url == "https://example.invalid/meeting-notes"
    assert config.webhook_auth_header == "Authorization"
    assert config.webhook_auth_value == "replace-me"
    assert "replace-me" in example
    assert "sk-" not in example
