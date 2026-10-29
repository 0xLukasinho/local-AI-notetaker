"""Atomic text and JSON writes.

A reader opening the destination path should see the previous complete file or
the new complete file, never a partially written one. The temporary file lives
in the destination directory so os.replace stays on the same volume.
"""

import json
import os
import tempfile


def atomic_write_text(path, text):
    """Write `text` to `path` via a temp file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".partial")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, payload):
    """Serialize `payload` as JSON and write it atomically."""
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    text += "\n"
    atomic_write_text(path, text)
