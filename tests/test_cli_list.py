import meeting_notes.cli as cli


def test_cmd_list_uses_library(tmp_path, monkeypatch, capsys):
    d = tmp_path / "2026-07-01_standup"
    d.mkdir()
    (d / "notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "2026-06-30_kickoff").mkdir()

    monkeypatch.setattr(cli, "OUTPUT_DIR", str(tmp_path))
    cli.cmd_list(None)

    out = capsys.readouterr().out
    assert "2026-07-01  Standup  (notes ready)" in out
    assert "2026-06-30  Kickoff  (no notes)" in out
    # most recent first
    assert out.index("Standup") < out.index("Kickoff")
