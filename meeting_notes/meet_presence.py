"""Google Meet presence from the Chrome extension.

The content script on meet.google.com posts a heartbeat to 127.0.0.1.
A fresh in-call heartbeat is enough to start a recording. Window titles stay
a second signal for when the extension is not loaded. The listener only
accepts connections on the loopback address it bound.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from meeting_notes.meet_detect import Sighting, extract_meet_code
from meeting_notes.session import slugify


# Heartbeats are about every 3 seconds. A few missed posts are not a leave.
# The watcher's own leave debounce (default 25s) is what ends the session
# after this signal drops.
FRESH_SECONDS = 10
_MAX_BODY = 8192


def presence_port(speaker_bridge_port):
    """Sibling of the speaker bridge so a recording can still bind that port."""
    port = int(speaker_bridge_port) + 1
    if port > 65535:
        port = int(speaker_bridge_port) - 1
    if port < 1:
        raise ValueError("presence port is out of range")
    return port



def _presence_names(raw):
    """Display names from the extension payload. Never invents a name."""
    names = []
    seen = set()
    for item in raw or []:
        if isinstance(item, str):
            name = " ".join(item.split())
        elif isinstance(item, dict):
            name = " ".join(str(item.get("name") or "").split())
        else:
            continue
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def parse_presence(body):
    """Normalize one heartbeat. None when it is not an object."""
    if not isinstance(body, dict):
        return None
    phase = str(body.get("phase") or "").strip().lower()
    in_call = body.get("in_call")
    if phase not in ("in-call", "lobby", "left"):
        phase = "in-call" if in_call is True else "left"
    code = extract_meet_code(str(body.get("meet_code") or "")) or ""
    title = " ".join(str(body.get("title") or "").split())
    visible = body.get("visible")
    participants = _presence_names(body.get("participants"))
    return {
        "meet_code": code,
        "phase": phase,
        "in_call": phase == "in-call" and bool(code or title or participants),
        "visible": bool(visible) if visible is not None else None,
        "title": title[:200],
        "participants": participants,
    }


def sighting_from_presence(parsed):
    """A Meet sighting for an in-call heartbeat, else None."""
    if not parsed or not parsed.get("in_call"):
        return None
    code = parsed.get("meet_code") or ""
    title = parsed.get("title") or ""
    if code:
        display = code
        key = code
    else:
        display = title or "Google Meet"
        key = slugify(display) or "google-meet"
    window = title or ("Meet - %s" % display)
    return Sighting(
        key=key,
        display_name=display,
        meet_code=code or None,
        window_title=window,
        source="google-meet",
        participants=tuple(parsed.get("participants") or ()),
    )


class PresenceBook:
    """The latest heartbeat. Thread-safe; the HTTP handler and the watcher share it."""

    def __init__(self, fresh_seconds=FRESH_SECONDS, clock=None):
        self.fresh_seconds = float(fresh_seconds)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._record = None

    def update(self, body, now=None):
        parsed = parse_presence(body)
        if parsed is None:
            return None
        moment = self._clock() if now is None else float(now)
        record = dict(parsed)
        record["unix"] = moment
        with self._lock:
            self._record = record
        return record

    def latest(self):
        with self._lock:
            if self._record is None:
                return None
            return dict(self._record)

    def sighting(self, now=None):
        record = self.latest()
        if not record or not record.get("in_call"):
            return None
        moment = self._clock() if now is None else float(now)
        if moment - float(record.get("unix") or 0) > self.fresh_seconds:
            return None
        return sighting_from_presence(record)

    def last_seen_unix(self):
        record = self.latest()
        if not record:
            return None
        return record.get("unix")


class PresenceServer:
    """POST /presence on 127.0.0.1. Other paths are 404."""

    def __init__(self, book, port):
        self.book = book
        self.port = int(port)
        self.httpd = None
        self._thread = None

    def start(self):
        book = self.book

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def do_OPTIONS(self):
                self._send(204, b"")

            def do_GET(self):
                if self.path.split("?", 1)[0] != "/health":
                    self._send(404, b'{"ok":false}')
                    return
                self._send(200, b'{"ok":true,"role":"presence"}')

            def do_POST(self):
                if self.path.split("?", 1)[0] != "/presence":
                    self._send(404, b'{"ok":false}')
                    return
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                if host not in ("127.0.0.1", "localhost", ""):
                    self._send(403, b'{"ok":false}')
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length < 0 or length > _MAX_BODY:
                    self._send(413, b'{"ok":false}')
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeError, json.JSONDecodeError):
                    self._send(400, b'{"ok":false}')
                    return
                if book.update(payload) is None:
                    self._send(400, b'{"ok":false}')
                    return
                self._send(200, b'{"ok":true}')

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "https://meet.google.com")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.close_connection = True
                if body and status != 204:
                    self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self.httpd = server
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name="meet-presence", daemon=True)
        thread.start()
        self._thread = thread
        return self

    def stop(self):
        server = self.httpd
        self.httpd = None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
