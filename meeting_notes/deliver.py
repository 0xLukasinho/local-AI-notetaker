"""Webhook delivery for a finished session.

POSTs a JSON body with retries. Failures are written to an outbox directory
and retried later. The auth header value is read from config at send time and
is never written to the outbox or to status.json.
"""

import hashlib
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


# One JSON body stays under a typical 8 KB first-read / field limit. Two calls
# were stored cut off mid-sentence around 7–9 minutes while transcript.txt on
# disk was intact (~32 KB), which is what a receiver that keeps only the first
# chunk of a larger body looks like. Each piece is a complete JSON document.
# 0 disables the limit and sends the transcript in one request.
DEFAULT_MAX_BODY_BYTES = 6000
_MIN_BODY_BYTES = 2048


def chunk_body_limit(config):
    """Byte budget for one POST body. ``None`` means send a single body."""
    raw = getattr(config, "webhook_max_body_bytes", DEFAULT_MAX_BODY_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY_BYTES
    if value <= 0:
        return None
    if value < _MIN_BODY_BYTES:
        return _MIN_BODY_BYTES
    return value


def redact_secret(text, secret):
    """Remove the webhook credential from an error string before it is stored."""
    if text is None or not secret or len(str(secret)) < 4:
        return text
    return str(text).replace(str(secret), "[redacted]")


def split_transcript_to_fit(text, fits):
    """Split ``text`` so each piece satisfies ``fits``, keeping line breaks.

    ``fits(piece)`` is true when that string is small enough. Pieces join back
    to ``text``. A line longer than the budget is split mid-line so progress
    is always made.
    """
    if text == "":
        return [""]
    if fits(text):
        return [text]
    parts = []
    rest = text
    while rest:
        if fits(rest):
            parts.append(rest)
            break
        take = _largest_fitting_prefix(rest, fits)
        newline = rest.rfind("\n", 0, take)
        if newline > 0 and fits(rest[: newline + 1]):
            take = newline + 1
        if take < 1:
            take = 1
        parts.append(rest[:take])
        rest = rest[take:]
    return parts


def _largest_fitting_prefix(text, fits):
    if not text:
        return 0
    if not fits(text[:1]):
        return 1
    best = 1
    left = 1
    right = len(text)
    while left <= right:
        mid = (left + right) // 2
        if fits(text[:mid]):
            best = mid
            left = mid + 1
        else:
            right = mid - 1
    return best


def _estimate_body_bytes(payload, piece, total_chars, digest):
    """Upper bound on the JSON body size for this transcript piece.

    Chunk indexes are estimated with three digits so the real body (smaller
    indexes) cannot grow past a piece that passed this check.
    """
    trial = dict(payload)
    trial["transcript"] = piece
    trial["chunk_index"] = 999
    trial["chunk_count"] = 999
    trial["transcript_sha256"] = digest
    trial["transcript_chars"] = total_chars
    return len(json.dumps(trial, ensure_ascii=False).encode("utf-8"))


def iter_delivery_bodies(payload, max_body_bytes):
    """Yield POST bodies. ``transcript`` is one piece; the other fields repeat.

    ``chunk_index`` / ``chunk_count`` put the piece back in order.
    ``transcript_sha256`` and ``transcript_chars`` describe the full transcript.
    A transcript that already fits is one body with ``chunk_count`` 1.
    """
    text = payload.get("transcript")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    total_chars = len(text)

    def fits(piece):
        return _estimate_body_bytes(payload, piece, total_chars, digest) <= max_body_bytes

    if max_body_bytes is None:
        parts = [text]
    else:
        parts = split_transcript_to_fit(text, fits)
    count = len(parts)
    for index, part in enumerate(parts):
        body = dict(payload)
        body["transcript"] = part
        body["chunk_index"] = index
        body["chunk_count"] = count
        body["transcript_sha256"] = digest
        body["transcript_chars"] = total_chars
        yield body


def send_payload(config, payload, poster, sleep, log=None):
    """POST every chunk. Returns ``(ok, error)``. The error never contains the key."""
    limit = chunk_body_limit(config)
    bodies = list(iter_delivery_bodies(payload, limit))
    total = len(bodies)
    chars = bodies[0].get("transcript_chars", 0) if bodies else 0
    if log is not None:
        if limit is None:
            log.info("Posting transcript (%s chars, 1 request)", chars)
        else:
            log.info(
                "Posting transcript (%s chars, %s request(s), max body %s bytes)",
                chars,
                total,
                limit,
            )
    for body in bodies:
        ok, error = post_with_retry(config, body, poster, sleep)
        if not ok:
            error = redact_secret(error, getattr(config, "webhook_auth_value", ""))
            index = int(body.get("chunk_index") or 0) + 1
            detail = f"chunk {index}/{total}: {error}"
            if log is not None:
                log.error("Transcript POST failed: %s", detail)
            return False, detail
    return True, None


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


def deliver_folder(folder, config, poster=None, sleep=None, outbox=None, control_dir=None, log=None):
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
    ok, error = send_payload(config, payload, poster, sleep, log=log)
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


def retry_outbox(directory, config, poster=None, sleep=None, log=None):
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
        ok, error = send_payload(config, payload, poster, sleep, log=log)
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
