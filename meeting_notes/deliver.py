"""Webhook delivery for a finished session.

POSTs a JSON body with retries. Failures are written to an outbox directory
and retried later. The auth header value is read from config at send time and
is never written to the outbox or to status.json.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

from meeting_notes.atomic_io import atomic_write_json
from meeting_notes.session import (
    iso_now,
    outbox_dir,
    read_status,
    transcript_path,
    update_status,
)


class DeliveryError(Exception):
    pass


def build_payload(status, transcript, folder):
    status = status or {}
    folder = os.path.abspath(folder)
    joined = status.get("joined_at")
    left = status.get("left_at") or status.get("recording_stopped_at") or status.get("finished_at")
    return {
        "folder_name": os.path.basename(folder),
        "folder_path": folder,
        "meet_code": status.get("meet_code"),
        "window_title": status.get("window_title"),
        "joined_at": joined,
        "left_at": left,
        "duration_seconds": duration_seconds(joined, left),
        "whisper_model": status.get("whisper_model"),
        "language": status.get("language"),
        "transcript": transcript,
    }


def duration_seconds(joined_at, left_at):
    start = _parse_iso(joined_at)
    end = _parse_iso(left_at)
    if start is None or end is None:
        return None
    return int((end - start).total_seconds())


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def urllib_poster(url, payload, header_name, header_value, timeout):
    """POST JSON. Raises DeliveryError on any HTTP or network failure."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if header_name and header_value:
        request.add_header(header_name, header_value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raise DeliveryError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise DeliveryError(f"request failed: {reason}") from exc
    if status >= 400:
        raise DeliveryError(f"HTTP {status}")
    return status


def post_with_retry(config, payload, poster, sleep, attempts=None, backoff=None):
    """Try `poster` up to `attempts` times. Sleep doubles between tries.

    Returns (True, None) or (False, error_message).
    """
    attempts = config.delivery_attempts if attempts is None else attempts
    backoff = config.delivery_backoff_seconds if backoff is None else backoff
    if attempts < 1:
        attempts = 1
    last_error = None
    for index in range(attempts):
        try:
            poster(
                config.webhook_url,
                payload,
                config.webhook_auth_header,
                config.webhook_auth_value,
                config.delivery_timeout_seconds,
            )
            return True, None
        except DeliveryError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
        if index + 1 < attempts:
            delay = float(backoff) * (2 ** index)
            sleep(delay)
    return False, last_error or "delivery failed"


def deliver_folder(folder, config, poster=None, sleep=None, outbox=None, control_dir=None):
    """POST the session transcript. On failure, write an outbox entry.

    Returns True on success. A missing webhook URL is a skip (False) and is
    not written to the outbox — there is nothing to retry until the URL exists.
    HTTP failures are written to the outbox.
    """
    poster = poster or urllib_poster
    sleep = sleep or time.sleep
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise DeliveryError(f"meeting folder not found: {folder}")
    path = transcript_path(folder)
    if not os.path.isfile(path):
        raise DeliveryError(f"no transcript at {path}")
    with open(path, "r", encoding="utf-8") as handle:
        transcript = handle.read()
    status = read_status(folder) or {}
    payload = build_payload(status, transcript, folder)
    if not config.webhook_url:
        return False
    ok, error = post_with_retry(config, payload, poster, sleep)
    if ok:
        update_status(
            folder,
            {
                "delivered": True,
                "delivered_at": iso_now(),
                "delivery_error": None,
            },
            preserve=(),
        )
        if config.delete_audio_after_delivery:
            _delete_audio(folder)
        if outbox is None and control_dir is not None:
            outbox = outbox_dir(control_dir)
        if outbox:
            _remove_outbox_entry(outbox, payload["folder_name"])
        return True
    if outbox is None and control_dir is not None:
        outbox = outbox_dir(control_dir)
    if outbox:
        write_outbox(outbox, payload, error)
    update_status(
        folder,
        {"delivered": False, "delivery_error": error},
        preserve=("delivered_at",),
    )
    return False


def write_outbox(directory, payload, error):
    os.makedirs(directory, exist_ok=True)
    name = payload.get("folder_name") or "session"
    path = os.path.join(directory, f"{name}.json")
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, json.JSONDecodeError):
            existing = {}
    body = {
        "folder_name": payload.get("folder_name"),
        "folder_path": payload.get("folder_path"),
        "payload": payload,
        "last_error": error,
        "attempts": int(existing.get("attempts") or 0) + 1,
        "created_at": existing.get("created_at") or iso_now(),
        "updated_at": iso_now(),
    }
    atomic_write_json(path, body)
    return path


def _remove_outbox_entry(directory, folder_name):
    if not folder_name:
        return
    path = os.path.join(directory, f"{folder_name}.json")
    try:
        os.remove(path)
    except OSError:
        pass


def _delete_audio(folder):
    path = os.path.join(folder, "audio.wav")
    try:
        os.remove(path)
    except OSError:
        pass


def retry_outbox(directory, config, poster=None, sleep=None):
    """Retry every outbox file once (each try still uses post_with_retry).

    Returns the number of successful deliveries. Does nothing when no webhook
    is configured, leaving the files in place.
    """
    if not config.webhook_url or not directory or not os.path.isdir(directory):
        return 0
    poster = poster or urllib_poster
    sleep = sleep or time.sleep
    delivered = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                item = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        ok, error = post_with_retry(config, payload, poster, sleep)
        if ok:
            folder = payload.get("folder_path")
            if folder and os.path.isdir(folder):
                update_status(
                    folder,
                    {
                        "delivered": True,
                        "delivered_at": iso_now(),
                        "delivery_error": None,
                    },
                    preserve=(),
                )
                if config.delete_audio_after_delivery:
                    _delete_audio(folder)
            try:
                os.remove(path)
            except OSError:
                pass
            delivered += 1
        else:
            item["last_error"] = error
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["updated_at"] = iso_now()
            try:
                atomic_write_json(path, item)
            except OSError:
                pass
    return delivered
