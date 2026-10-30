"""Session audio-device log shared by the recorder and the watcher.

The recorder appends one line per device event and prints it (the watcher
redirects that process's stdout to ``recorder.log``). The watcher tails this
file into ``watch.log`` so a headset change is visible in both places.
"""

import os
import threading


DEVICE_LOG_FILENAME = "audio-devices.log"


class DeviceLog:
    """Append-only device event log in a session folder."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, DEVICE_LOG_FILENAME)
        self._lock = threading.Lock()

    def event(self, message):
        line = " ".join(str(message).split())
        if not line:
            return
        with self._lock:
            _safe_print(f"audio: {line}")
            try:
                os.makedirs(self.folder, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError:
                pass


def read_new_device_events(path, offset):
    """Read complete new lines from ``path`` starting at byte ``offset``.

    A trailing partial line is left for the next read. Returns
    ``(lines, new_offset)``. A missing file yields no lines and the same offset.
    """
    offset = int(offset or 0)
    if offset < 0:
        offset = 0
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            if offset > end:
                offset = 0
            handle.seek(offset)
            raw = handle.read()
    except OSError:
        return [], offset
    if not raw:
        return [], offset
    if not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        if cut == -1:
            return [], offset
        raw = raw[: cut + 1]
    text = raw.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return lines, offset + len(raw)


def _safe_print(message):
    """Print when a console or log file is attached.

    ``pythonw.exe`` has no stdout unless the parent redirected it. A failed
    print must not tear down capture.
    """
    try:
        print(message, flush=True)
    except Exception:
        pass
