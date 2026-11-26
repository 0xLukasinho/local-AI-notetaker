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


@pytest.fixture(autouse=True)
def _never_touch_the_live_launchagent(monkeypatch, tmp_path_factory):
    """Tests must never load, unload, or rewrite the real LaunchAgent.

    HOME points at a temp dir (so ~/Library/LaunchAgents and ~/.meeting-notes
    resolve there) and any real `launchctl` call fails the test.
    """
    import subprocess

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _argv0(args, kwargs):
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, (list, tuple)) and argv:
            return str(argv[0])
        if isinstance(argv, str):
            return argv.split()[0] if argv.split() else ""
        return ""

    def guarded_run(*args, **kwargs):
        if _argv0(args, kwargs).endswith("launchctl"):
            raise AssertionError("test tried to run the real launchctl")
        return real_run(*args, **kwargs)

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            if _argv0(args, kwargs).endswith("launchctl"):
                raise AssertionError("test tried to run the real launchctl")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
