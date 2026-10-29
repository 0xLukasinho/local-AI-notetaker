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
    ):
        monkeypatch.delenv(key, raising=False)
