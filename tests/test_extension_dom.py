"""content.js against a Meet DOM with captions off (needs node + jsdom)."""

import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "js", "meet_dom_captions_off.js")


def _jsdom_available():
    node = shutil.which("node")
    if not node:
        return False
    probe = subprocess.run([node, "-e", "require('jsdom')"], capture_output=True, env=os.environ)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _jsdom_available(), reason="node with jsdom (NODE_PATH) not available")


def _sample(speaker):
    out = subprocess.run(["node", SCRIPT, speaker], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_names_come_from_the_tile_label_without_aria_label_or_captions():
    sample = _sample("1")
    names = {p["name"]: p["self"] for p in sample["participants"]}
    assert names.get("Alex Rivera") is False
    assert names.get("Priya Shah") is False
    # The self tile's "You" label marks self; it is never a remote person.
    assert "You" not in names
    assert sample["captions_enabled"] is False
    assert not sample.get("captions")


def test_active_speaker_follows_indicator_activity_with_captions_off():
    sample = _sample("1")
    assert [a["name"] for a in sample["active"]] == ["Alex Rivera"]
    sample = _sample("2")
    assert [a["name"] for a in sample["active"]] == ["Priya Shah"]


def test_self_speaking_is_reported_as_self():
    sample = _sample("3")
    assert sample["active"] and all(a["self"] for a in sample["active"])
