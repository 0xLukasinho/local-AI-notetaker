"""Localhost receiver for Meet speaker samples.

The Chrome extension POSTs the active speaker, the participant roster, and
any live-caption lines here while a recording is running. Nothing is accepted
from any address other than the loopback bind. Samples are stamped with
server time, in seconds since `mark_audio_start`, and appended to
speakers.jsonl.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from meeting_notes.speakers import coerce_event


# Caption snapshots ride along with the speaker sample. A few Meet lines fit
# well under this; the cap is what stops a hostile page from filling the disk.
_MAX_BODY = 262144


class SpeakerBridge:
    def __init__(self, path, port=8765, clock=None):
        self.path = path
        self.port = int(port)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._audio_start = None
        self._pending = []
        self.httpd = None
        self._thread = None

    def start(self):
        bridge = self

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
                body = json.dumps({"ok": True, "port": bridge.bound_port}).encode("utf-8")
                self._send(200, body, "application/json")

            def do_POST(self):
                if self.path.split("?", 1)[0] != "/speakers":
                    self._send(404, b'{"ok":false}')
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
                if not isinstance(payload, dict):
                    self._send(400, b'{"ok":false}')
                    return
                if bridge.record(payload) is None:
                    self._send(400, b'{"ok":false}')
                    return
                self._send(200, b'{"ok":true}', "application/json")

            def _send(self, status, body, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.close_connection = True
                if body:
                    self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self.httpd = server
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name="speaker-bridge", daemon=True)
        thread.start()
        self._thread = thread
        return self

    @property
    def bound_port(self):
        if self.httpd is None:
            return self.port
        return self.httpd.server_address[1]

    def mark_audio_start(self):
        """Start the transcript clock. Samples received before this are rewritten."""
        with self._lock:
            self._audio_start = self._clock()
            pending = self._pending
            self._pending = []
            for moment, body in pending:
                self._append(moment, body)

    def record(self, body):
        """Append one sample. Returns the stored event, or a queued marker."""
        now = self._clock()
        with self._lock:
            if self._audio_start is None:
                self._pending.append((now, dict(body)))
                return {"queued": True}
            return self._append(now, body)

    def _append(self, now, body):
        """Write one sample. Caller holds the lock and the audio clock is set."""
        stamped = dict(body)
        stamped["t"] = round(now - self._audio_start, 3)
        event = coerce_event(stamped)
        if event is None:
            return None
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return event

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
