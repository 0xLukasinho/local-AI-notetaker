import pytest


@pytest.fixture(autouse=True)
def _isolate_meeting_notes_env(monkeypatch):
    """Keep developer webhook env vars from changing unit-test defaults."""
    for key in (
        "MEETING_NOTES_CONFIG",
        "MEETING_NOTES_WEBHOOK_URL",
        "MEETING_NOTES_WEBHOOK_AUTH_HEADER",
        "MEETING_NOTES_WEBHOOK_AUTH_VALUE",
        "MEETING_NOTES_MODEL",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _inline_watcher_background(monkeypatch):
    """Run watcher background work inline and never spawn a real process."""
    import meeting_notes.watcher as watcher_module

    monkeypatch.setattr(watcher_module, "_default_background", lambda: (lambda fn: fn()))
    monkeypatch.setattr(
        watcher_module,
        "_default_resumer",
        lambda: (lambda folder: (_ for _ in ()).throw(AssertionError("unexpected process spawn: %s" % folder))),
    )
