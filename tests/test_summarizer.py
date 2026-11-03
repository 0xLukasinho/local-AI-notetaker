import subprocess

import meeting_notes.summarizer as summarizer


def _fake_run(returncode=0, stdout="", stderr=""):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return fake_run


def test_failure_surfaces_stdout_error(monkeypatch, capsys):
    """claude -p prints errors (e.g. auth failures) to stdout — show them."""
    monkeypatch.setattr(summarizer.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(
        summarizer.subprocess,
        "run",
        _fake_run(returncode=1, stdout="Invalid API key · Please run /login"),
    )

    notes = summarizer.generate_notes("hello", "Test", "2026-08-11")

    assert notes is None
    err = capsys.readouterr().err
    assert "Invalid API key" in err


def test_auth_failure_prints_login_hint(monkeypatch, capsys):
    monkeypatch.setattr(summarizer.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(
        summarizer.subprocess,
        "run",
        _fake_run(returncode=1, stdout="Invalid API key · Please run /login"),
    )

    summarizer.generate_notes("hello", "Test", "2026-08-11")

    err = capsys.readouterr().err
    assert "claude" in err and "login" in err.lower()


def test_failure_still_surfaces_stderr(monkeypatch, capsys):
    monkeypatch.setattr(summarizer.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(
        summarizer.subprocess,
        "run",
        _fake_run(returncode=1, stderr="some stderr detail"),
    )

    notes = summarizer.generate_notes("hello", "Test", "2026-08-11")

    assert notes is None
    err = capsys.readouterr().err
    assert "some stderr detail" in err


def test_success_returns_notes(monkeypatch):
    monkeypatch.setattr(summarizer.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(
        summarizer.subprocess,
        "run",
        _fake_run(returncode=0, stdout="**Summary**\n- all good\n"),
    )

    notes = summarizer.generate_notes("hello", "Test", "2026-08-11")

    assert notes == "**Summary**\n- all good"
