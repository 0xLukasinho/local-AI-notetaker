from meeting_notes.device_log import DeviceLog, read_new_device_events


def test_device_log_appends_and_prints(tmp_path, capsys):
    log = DeviceLog(str(tmp_path))
    log.event("capturing microphone 'Headset'")
    log.event("  default output switched:  'A' -> 'B'  ")
    text = (tmp_path / "audio-devices.log").read_text(encoding="utf-8")
    assert text == (
        "capturing microphone 'Headset'\n"
        "default output switched: 'A' -> 'B'\n"
    )
    printed = capsys.readouterr().out
    assert "audio: capturing microphone 'Headset'" in printed
    assert "audio: default output switched: 'A' -> 'B'" in printed


def test_reader_holds_a_partial_line(tmp_path):
    path = tmp_path / "audio-devices.log"
    path.write_bytes(b"capturing microphone 'Mic'\nopened loopback")
    lines, offset = read_new_device_events(str(path), 0)
    assert lines == ["capturing microphone 'Mic'"]
    lines, offset = read_new_device_events(str(path), offset)
    assert lines == []
    with path.open("ab") as handle:
        handle.write(b" 'Headphones' (48000 Hz, 2 ch, default output)\n")
    lines, offset = read_new_device_events(str(path), offset)
    assert lines == ["opened loopback 'Headphones' (48000 Hz, 2 ch, default output)"]
    lines, offset = read_new_device_events(str(path), offset)
    assert lines == []
    assert offset == path.stat().st_size


def test_reader_rewinds_when_the_file_is_replaced(tmp_path):
    path = tmp_path / "audio-devices.log"
    path.write_text("first\n", encoding="utf-8")
    _lines, offset = read_new_device_events(str(path), 0)
    path.write_text("replaced\n", encoding="utf-8")
    lines, _offset = read_new_device_events(str(path), offset + 100)
    assert lines == ["replaced"]
