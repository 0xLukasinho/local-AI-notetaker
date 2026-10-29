"""User configuration for automation.

The file lives outside the repo, at ~/.meeting-notes/config.toml (or
config.json). Secrets belong there or in environment variables, never in git.

Manual `start` / `reprocess` keep today's behavior when this file is absent:
transcript_only defaults to false, so Claude notes are still generated.
"""

import json
import os
from dataclasses import dataclass, fields


class ConfigError(Exception):
    """Raised when a config file exists but cannot be parsed."""


ENV_OVERRIDES = {
    "MEETING_NOTES_WEBHOOK_URL": "webhook_url",
    "MEETING_NOTES_WEBHOOK_AUTH_HEADER": "webhook_auth_header",
    "MEETING_NOTES_WEBHOOK_AUTH_VALUE": "webhook_auth_value",
}


def default_control_dir():
    return os.path.expanduser("~/.meeting-notes")


@dataclass
class Config:
    webhook_url: str = ""
    webhook_auth_header: str = "Authorization"
    webhook_auth_value: str = ""
    join_debounce_seconds: float = 12
    leave_debounce_seconds: float = 25
    poll_interval_seconds: float = 2
    outbox_retry_seconds: float = 60
    # CLI start/reprocess. False preserves the historical Claude summary.
    transcript_only: bool = False
    # Watcher-started sessions. Independent of the CLI default.
    watcher_transcript_only: bool = True
    delete_audio_after_delivery: bool = False
    # Empty means the built-in "Meet - ..." Chrome title pattern.
    window_title_regex: str = ""
    mic_process_marker: str = "chrome.exe"
    whisper_model: str = "medium"
    language: str = "en"
    transcribe_timeout_seconds: float = 3 * 60 * 60
    delivery_attempts: int = 3
    delivery_backoff_seconds: float = 1.0
    delivery_timeout_seconds: float = 30


_FLOAT_FIELDS = {
    "join_debounce_seconds",
    "leave_debounce_seconds",
    "poll_interval_seconds",
    "outbox_retry_seconds",
    "transcribe_timeout_seconds",
    "delivery_backoff_seconds",
    "delivery_timeout_seconds",
}
_INT_FIELDS = {"delivery_attempts"}
_BOOL_FIELDS = {
    "transcript_only",
    "watcher_transcript_only",
    "delete_audio_after_delivery",
}
_KNOWN = {item.name for item in fields(Config)}


def load_config(control_dir=None, environ=None, path=None):
    """Load config. A missing file returns defaults and is not an error."""
    environ = os.environ if environ is None else environ
    if path is None:
        path = _resolve_path(control_dir, environ)
    data = {}
    if path and os.path.exists(path):
        data = _read_file(path)
    config = _from_mapping(data)
    _apply_env(config, environ)
    return config


def _resolve_path(control_dir, environ):
    override = environ.get("MEETING_NOTES_CONFIG") if environ else None
    if override:
        return override
    directory = control_dir or default_control_dir()
    toml_path = os.path.join(directory, "config.toml")
    json_path = os.path.join(directory, "config.json")
    if os.path.exists(toml_path):
        return toml_path
    if os.path.exists(json_path):
        return json_path
    return toml_path


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if path.lower().endswith(".json"):
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    else:
        parsed = _load_toml(text, path)
    if not isinstance(parsed, dict):
        raise ConfigError(f"config root must be a table/object: {path}")
    return parsed


def _load_toml(text, path):
    try:
        import tomllib
    except ImportError:
        try:
            return _parse_simple_toml(text)
        except ValueError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    try:
        return tomllib.loads(text) if text.strip() else {}
    except Exception as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _parse_simple_toml(text):
    """Parse a flat TOML document (root keys only).

    Enough for this project's config on Python versions without tomllib, and
    used directly by tests. Strings, booleans, ints, and floats are supported.
    """
    data = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno}: expected key = value")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"line {lineno}: empty key")
        data[key] = _parse_toml_value(value.strip(), lineno)
    return data


def _parse_toml_value(value, lineno):
    if value.startswith('"') or value.startswith("'"):
        quote = value[0]
        end = _matching_quote(value, quote)
        if end is None:
            raise ValueError(f"line {lineno}: unterminated string")
        body = value[1:end]
        if quote == '"':
            body = (
                body.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        return body
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        raise ValueError(f"line {lineno}: unsupported value {value!r}") from None


def _matching_quote(value, quote):
    escaped = False
    for index in range(1, len(value)):
        char = value[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return index
    return None


def _from_mapping(data):
    kwargs = {}
    for key, value in data.items():
        if key not in _KNOWN or value is None:
            continue
        kwargs[key] = _coerce(key, value)
    return Config(**kwargs)


def _coerce(key, value):
    if key in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key in _INT_FIELDS:
        return int(value)
    if key in _FLOAT_FIELDS:
        return float(value)
    return str(value)


def _apply_env(config, environ):
    for env_name, field in ENV_OVERRIDES.items():
        if env_name in environ:
            setattr(config, field, environ[env_name])
