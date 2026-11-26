"""Copy the unpacked Meet speaker extension into the control directory.

Chrome will not load an extension from an arbitrary path by itself. `install`
writes a folder the user loads once from chrome://extensions. The bridge port
from config is written into that copy.
"""

import glob
import hashlib
import json
import os

from meeting_notes.atomic_io import atomic_write_text


_FILES = ("manifest.json", "content.js", "background.js")
_PORT_LINE = "const BRIDGE_PORT = 8765;"
_PRESENCE_LINE = "const PRESENCE_PORT = 8766;"
EXTENSION_NAME = "Meeting Notes Speaker Tags"


def extension_source_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_extension")


def installed_extension_dir(control_dir):
    return os.path.join(control_dir, "chrome-extension")


def chrome_unpacked_extension_id(path):
    """Chrome's deterministic 32-char ID for an unpacked extension directory."""
    real = os.path.realpath(os.path.expanduser(path))
    digest = hashlib.sha256(real.encode("utf-8")).digest()[:16]
    chars = []
    for byte in digest:
        chars.append(chr(ord("a") + (byte >> 4)))
        chars.append(chr(ord("a") + (byte & 0x0F)))
    return "".join(chars)


def _default_chrome_dir():
    return os.path.expanduser("~/Library/Application Support/Google/Chrome")


def find_chrome_extension_installs(
    extension_name=EXTENSION_NAME,
    extension_id=None,
    chrome_dir=None,
):
    """Read Chrome Preferences / Secure Preferences for loaded copies.

    Never raises when macOS blocks App Data access; returns
    ``{"accessible": False, "entries": []}`` in that case.
    """
    base = chrome_dir or _default_chrome_dir()
    if not os.path.isdir(base):
        return {"accessible": False, "entries": []}
    candidates = []
    for pattern in ("Preferences", "Secure Preferences", "*/Preferences", "*/Secure Preferences"):
        candidates.extend(sorted(glob.glob(os.path.join(base, pattern))))
    if not candidates:
        return {"accessible": False, "entries": []}

    accessible = False
    entries = []
    seen = set()
    target_name = (extension_name or "").casefold()
    for pref_path in candidates:
        try:
            with open(pref_path, "r", encoding="utf-8", errors="replace") as handle:
                data = json.load(handle)
        except OSError:
            continue
        except ValueError:
            accessible = True
            continue
        accessible = True
        settings = ((data.get("extensions") or {}).get("settings") or {})
        if not isinstance(settings, dict):
            continue
        profile = os.path.basename(os.path.dirname(pref_path))
        for ext_id, info in settings.items():
            if not isinstance(info, dict):
                continue
            manifest = info.get("manifest") or {}
            name = str(manifest.get("name") or "")
            if extension_id and ext_id != extension_id:
                continue
            if not extension_id and target_name and name.casefold() != target_name:
                continue
            ext_path = str(info.get("path") or "")
            key = (profile, ext_id, ext_path)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "profile": profile,
                    "id": ext_id,
                    "name": name,
                    "version": str(manifest.get("version") or ""),
                    "path": ext_path,
                }
            )
    return {"accessible": accessible, "entries": entries}


def _manifest_version(folder):
    manifest = os.path.join(folder, "manifest.json")
    if not os.path.isfile(manifest):
        return ""
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except (OSError, ValueError):
        return ""


def _write_extension_files(source, destination, port):
    os.makedirs(destination, exist_ok=True)
    presence_port = port + 1 if port < 65535 else port - 1
    for name in _FILES:
        src = os.path.join(source, name)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"extension file missing: {src}")
        with open(src, "r", encoding="utf-8") as handle:
            text = handle.read()
        if name == "background.js":
            if _PORT_LINE not in text:
                raise RuntimeError("background.js is missing the BRIDGE_PORT line")
            text = text.replace(_PORT_LINE, f"const BRIDGE_PORT = {port};", 1)
        if name == "content.js":
            if _PRESENCE_LINE not in text:
                raise RuntimeError("content.js is missing the PRESENCE_PORT line")
            text = text.replace(_PRESENCE_LINE, f"const PRESENCE_PORT = {presence_port};", 1)
        atomic_write_text(os.path.join(destination, name), text)


def install_extension(control_dir, port, target_dir=None, update_loaded=False, chrome_dir=None):
    """Copy the extension and return the folder Chrome should load."""
    port = int(port)
    if port < 1 or port > 65535:
        raise ValueError(f"speaker bridge port is out of range: {port}")
    source = extension_source_dir()
    destination = os.path.abspath(target_dir) if target_dir else installed_extension_dir(control_dir)
    _write_extension_files(source, destination, port)
    if update_loaded:
        info = find_chrome_extension_installs(chrome_dir=chrome_dir)
        dest_real = os.path.realpath(destination)
        src_real = os.path.realpath(source)
        for entry in info.get("entries") or []:
            loaded = entry.get("path") or ""
            if not loaded or not os.path.isdir(loaded):
                continue
            loaded_real = os.path.realpath(loaded)
            if loaded_real in (dest_real, src_real):
                continue
            # Do not overwrite an active repo checkout; only update unpacked copies.
            if os.path.isfile(os.path.join(loaded_real, "manifest.json")) and not os.path.exists(
                os.path.join(os.path.dirname(os.path.dirname(loaded_real)), ".git")
            ):
                _write_extension_files(source, loaded, port)
    return destination


def inspect_extension_state(control_dir, chrome_dir=None):
    """Compare the installed copy against what Chrome actually loads."""
    folder = installed_extension_dir(control_dir)
    expected_id = chrome_unpacked_extension_id(folder)
    installed_version = _manifest_version(folder)
    chrome_info = find_chrome_extension_installs(chrome_dir=chrome_dir)
    folder_real = os.path.realpath(folder)
    mismatches = []
    for entry in chrome_info.get("entries") or []:
        loaded_path = entry.get("path") or ""
        loaded_real = os.path.realpath(loaded_path) if loaded_path else ""
        path_differs = bool(loaded_real and loaded_real != folder_real)
        version_differs = bool(installed_version and entry.get("version") and entry.get("version") != installed_version)
        if path_differs or version_differs:
            mismatches.append(
                {
                    **entry,
                    "path_differs": path_differs,
                    "version_differs": version_differs,
                    "disk_version": _manifest_version(loaded_path) if loaded_path else "",
                }
            )
    return {
        "installed_dir": folder,
        "installed_id": expected_id,
        "installed_version": installed_version,
        "chrome_accessible": chrome_info["accessible"],
        "loaded_entries": chrome_info["entries"],
        "mismatches": mismatches,
    }
