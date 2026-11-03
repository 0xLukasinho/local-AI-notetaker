"""Copy the unpacked Meet speaker extension into the control directory.

Chrome will not load an extension from an arbitrary path by itself. `install`
writes a folder the user loads once from chrome://extensions. The bridge port
from config is written into that copy.
"""

import os

from meeting_notes.atomic_io import atomic_write_text


_FILES = ("manifest.json", "content.js", "background.js")
_PORT_LINE = "const BRIDGE_PORT = 8765;"


def extension_source_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_extension")


def installed_extension_dir(control_dir):
    return os.path.join(control_dir, "chrome-extension")


def install_extension(control_dir, port):
    """Copy the extension and return the folder Chrome should load."""
    port = int(port)
    if port < 1 or port > 65535:
        raise ValueError(f"speaker bridge port is out of range: {port}")
    source = extension_source_dir()
    destination = installed_extension_dir(control_dir)
    os.makedirs(destination, exist_ok=True)
    for name in _FILES:
        src = os.path.join(source, name)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"extension file missing: {src}")
        with open(src, "r", encoding="utf-8") as handle:
            text = handle.read()
        if name == "background.js":
            needle = _PORT_LINE
            if needle not in text:
                raise RuntimeError("background.js is missing the BRIDGE_PORT line")
            text = text.replace(needle, f"const BRIDGE_PORT = {port};", 1)
        atomic_write_text(os.path.join(destination, name), text)
    return destination
