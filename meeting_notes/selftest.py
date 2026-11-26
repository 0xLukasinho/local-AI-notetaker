"""`meeting-notes selftest`: prove the Mac setup works, end to end.

Checks (each prints PASS / WARN / FAIL with the evidence):

- watcher: LaunchAgent installed with ProcessType Interactive, process alive,
  heartbeat fresh, scheduling priority not throttled;
- helper: which capture helper records (signed or legacy+shim), its signature;
- speaker models: sherpa-onnx and both ONNX models present;
- config: webhook / archive settings present (values are never printed);
- recent sessions: speaker source, kept audio, delivery;
- `--capture`: plays a three-voice clip through the speakers and records it
  through the real capture path (record_audio, the same call the watcher
  makes). Asserts a non-silent remote track and audio length >= 95% of wall
  clock;
- `--e2e`: additionally transcribes that recording and names the speakers
  (calendar-free: names come from what is said), asserting named lines.
- `--verify-signed`: runs the capture check with the signed helper and, on
  success, marks it verified so recording switches to it (no shim).

Refuses capture checks while a call is being recorded.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SCRIPT = os.path.join(os.path.dirname(__file__), "data", "e2e_script.txt")
EXPECTED_NAMES = {"Alex", "Mahmoud", "Ravi"}


class Report:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.items = []

    def add(self, status, name, detail):
        self.items.append((status, name, detail))
        print(f"[{status}] {name}: {detail}", file=self.out, flush=True)

    @property
    def ok(self):
        return all(status != FAIL for status, _, _ in self.items)


# --- helpers -------------------------------------------------------------

def control_dir():
    return os.path.expanduser("~/.meeting-notes")


def recording_now(home_control=None):
    """True when a call is being recorded (heartbeat or a live recorder)."""
    directory = home_control or control_dir()
    try:
        with open(os.path.join(directory, "heartbeat.json"), "r", encoding="utf-8") as handle:
            beat = json.load(handle)
        if str(beat.get("state", "")).lower() in ("recording", "joined", "in-call", "in_call"):
            return True
    except (OSError, ValueError):
        pass
    try:
        listing = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    for line in listing.splitlines():
        if "meeting-audio-capture record" in line or "meeting_notes.cli start" in line:
            if "selftest" not in line:
                return True
    return False


def wav_stats(path):
    """(seconds, rms, nonzero_fraction) of a 16-bit WAV."""
    import array

    with wave.open(path, "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.getnframes()
        data = handle.readframes(frames)
    samples = array.array("h")
    samples.frombytes(data[: len(data) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples) or 1
    total = 0
    nonzero = 0
    for value in samples:
        total += value * value
        if value:
            nonzero += 1
    rms = (total / count) ** 0.5
    return frames / float(rate or 1), rms, nonzero / float(count)


def build_clip(directory, script=SCRIPT, gap=0.6):
    """Render the multi-speaker script with macOS `say`; returns (wav, turns)."""
    lines = []
    with open(script, "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if raw and not raw.startswith("#") and "|" in raw:
                voice, text = raw.split("|", 1)
                lines.append((voice.strip(), text.strip()))
    import array

    pieces = []
    turns = []
    cursor = 0.0
    for index, (voice, text) in enumerate(lines):
        aiff = os.path.join(directory, f"{index:02d}.aiff")
        part = os.path.join(directory, f"{index:02d}.wav")
        subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True, timeout=60)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, part], check=True, timeout=60)
        with wave.open(part, "rb") as handle:
            data = handle.readframes(handle.getnframes())
            seconds = handle.getnframes() / 16000.0
        pieces.append(data)
        pieces.append(b"\0\0" * int(gap * 16000))
        turns.append({"voice": voice, "text": text, "start": round(cursor, 2), "end": round(cursor + seconds, 2)})
        cursor += seconds + gap
    clip = os.path.join(directory, "clip.wav")
    with wave.open(clip, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"".join(pieces))
    return clip, turns


def capture_clip(folder, clip, helper=None, lead=1.5, tail=1.5):
    """Record through `record_audio` while `afplay` plays `clip`.

    Returns a dict with wall-clock and per-track stats.
    """
    from meeting_notes import recorder_mac

    env_key = "MEETING_NOTES_CAPTURE_HELPER"
    previous = os.environ.get(env_key)
    if helper:
        os.environ[env_key] = helper
    try:
        output = os.path.join(folder, "audio.wav")
        started = time.monotonic()
        process = recorder_mac.record_audio(output, separate_tracks=True)
        time.sleep(lead)
        subprocess.run(["afplay", clip], check=True, timeout=600)
        time.sleep(tail)
        recorder_mac.stop_recording(process)
        wall = time.monotonic() - started
    finally:
        if helper:
            if previous is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = previous
    result = {"wall_seconds": round(wall, 2), "tracks": {}}
    for name in ("audio.wav", "audio-self.wav", "audio-others.wav"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            seconds, rms, nonzero = wav_stats(path)
            result["tracks"][name] = {
                "seconds": round(seconds, 2),
                "rms": round(rms, 1),
                "nonzero": round(nonzero, 3),
                "ratio": round(seconds / wall, 3) if wall else 0.0,
            }
    return result


def judge_capture(result, report, min_ratio=0.95):
    others = result["tracks"].get("audio-others.wav")
    if not others:
        report.add(FAIL, "capture", "audio-others.wav was not written")
        return False
    ok = True
    if others["rms"] < 20 or others["nonzero"] < 0.05:
        report.add(FAIL, "remote track", f"silent (rms {others['rms']}, non-zero {others['nonzero']:.1%})")
        ok = False
    else:
        report.add(PASS, "remote track", f"rms {others['rms']}, non-zero samples {others['nonzero']:.1%}")
    if others["ratio"] < min_ratio:
        report.add(FAIL, "length", f"{others['seconds']} s audio for {result['wall_seconds']} s wall clock ({others['ratio']:.1%} < {min_ratio:.0%})")
        ok = False
    else:
        report.add(PASS, "length", f"{others['seconds']} s audio for {result['wall_seconds']} s wall clock ({others['ratio']:.1%})")
    return ok


def name_capture(folder, config, model=None):
    """Transcribe the captured session and name its speakers. Returns (text, document)."""
    from meeting_notes import session_finish

    with open(os.path.join(folder, "status.json"), "w", encoding="utf-8") as handle:
        json.dump({"mode": "manual", "window_title": "selftest - Slack", "source": "selftest"}, handle)
    class Cfg:
        pass

    cfg = Cfg()
    for key, value in vars(config).items():
        setattr(cfg, key, value)
    # Synthetic voices must never land in the real voice-profile store, and
    # the test must not depend on (or fetch) the calendar.
    cfg.diarize = True
    cfg.voice_profiles = False
    cfg.calendar_json_path = ""
    cfg.calendar_ics_url = ""
    cfg.transcript_archive_dir = ""
    cfg.speaker_labels = True
    cfg.self_name = getattr(config, "self_name", "You")
    text = session_finish.write_transcript(folder, cfg, model or getattr(config, "whisper_model", "small"), "en")
    with open(os.path.join(folder, "transcript.json"), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    return text, document


def judge_names(document, report, expected=EXPECTED_NAMES):
    smap = document.get("speaker_map") or {}
    confirmed = {entry.get("name") for entry in smap.values() if entry.get("status") == "confirmed"}
    from meeting_notes.speaker_naming import similar_name

    found = {name for name in expected if any(similar_name(name, (c or "").split(" ")[0]) for c in confirmed)}
    remote_lines = [s for s in document.get("segments") or [] if s.get("track") != "self"]
    unnamed = [s for s in remote_lines if str(s.get("speaker", "")).startswith("Speaker")]
    detail = ", ".join(
        f"{label} -> {entry.get('display')} ({entry.get('confidence')})" for label, entry in smap.items()
    )
    if found == set(expected):
        report.add(PASS, "named speakers", detail)
        if unnamed:
            report.add(WARN, "unnamed lines", f"{len(unnamed)} remote line(s) still Speaker N")
        return True
    report.add(FAIL, "named speakers", f"expected {sorted(expected)}, got {detail or 'nothing'}")
    return False


# --- static checks -------------------------------------------------------

def check_watcher(report):
    plist = os.path.expanduser("~/Library/LaunchAgents/com.meeting-notes.watch.plist")
    if not os.path.isfile(plist):
        report.add(FAIL, "LaunchAgent", f"{plist} missing (run scripts/install_macos.sh)")
        return
    import plistlib

    with open(plist, "rb") as handle:
        data = plistlib.load(handle)
    kind = data.get("ProcessType")
    report.add(PASS if kind == "Interactive" else FAIL, "LaunchAgent", f"ProcessType={kind}")
    try:
        with open(os.path.join(control_dir(), "heartbeat.json"), "r", encoding="utf-8") as handle:
            beat = json.load(handle)
        age = time.time() - float(beat.get("unix") or 0)
        pid = int(beat.get("pid") or 0)
        status = PASS if age < 60 else FAIL
        report.add(status, "watcher heartbeat", f"state={beat.get('state')} pid={pid} age={age:.0f}s")
        if pid:
            pri = subprocess.run(["ps", "-o", "pri=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if pri:
                report.add(PASS if int(pri) >= 20 else FAIL, "watcher priority", f"pri={pri} (Background would be 4)")
    except (OSError, ValueError) as exc:
        report.add(FAIL, "watcher heartbeat", f"unreadable ({exc.__class__.__name__})")


def check_helper(report):
    from meeting_notes.macos_capture import (
        find_capture_helper,
        is_signed_helper,
        refresh_signed_screen_recording,
        signed_helper_status,
    )

    refresh_signed_screen_recording()
    helper = find_capture_helper(purpose="record")
    if not helper:
        report.add(FAIL, "capture helper", "not installed")
        return
    if is_signed_helper(helper):
        report.add(PASS, "capture helper", f"signed helper (stable identity), verified {signed_helper_status().get('verified_at')}")
    else:
        shim = os.path.expanduser("~/.meeting-notes/shim/libtapfix.dylib")
        if os.path.isfile(shim):
            report.add(WARN, "capture helper", "legacy ad-hoc helper + tap-fix shim (works; switch with `selftest --verify-signed`)")
        else:
            report.add(WARN, "capture helper", "legacy ad-hoc helper without the shim: built before the isExclusive fix it records silence")
    try:
        requirement = subprocess.run(["codesign", "-d", "-r-", helper], capture_output=True, text=True, timeout=10)
        lines = (requirement.stdout + "\n" + requirement.stderr).splitlines()
        designated = [line.lstrip("# ").strip() for line in lines if "designated" in line]
        report.add(PASS, "helper signature", designated[0] if designated else "unsigned or unreadable")
    except Exception:
        pass


def check_models(report):
    from meeting_notes.diarize import model_paths, sherpa_available

    if sherpa_available():
        report.add(PASS, "speaker models", "sherpa-onnx + pyannote segmentation + WeSpeaker embedding")
    else:
        seg, emb = model_paths()
        report.add(FAIL, "speaker models", f"missing (need sherpa-onnx, {seg}, {emb}); run `meeting-notes models --download`")


def check_config(report, config):
    report.add(PASS if config.webhook_url else WARN, "webhook", "configured" if config.webhook_url else "not configured")
    archive = os.path.expanduser(config.transcript_archive_dir or "")
    if archive:
        report.add(PASS if os.path.isdir(archive) else FAIL, "transcript archive", archive)
    else:
        report.add(WARN, "transcript archive", "off (set transcript_archive_dir for a complete copy per call)")
    report.add(PASS if config.diarize else WARN, "diarize", str(config.diarize))
    path = os.path.expanduser(config.calendar_json_path or "")
    if config.calendar_ics_url:
        report.add(PASS, "calendar roster", "private iCal URL configured")
    elif path and os.path.isfile(path):
        age = (time.time() - os.path.getmtime(path)) / 3600.0
        report.add(PASS if age < 48 else WARN, "calendar roster", f"{path} (updated {age:.0f} h ago)")
    elif path:
        report.add(WARN, "calendar roster", f"{path} not there yet: names come from titles, intros, addressing, voice profiles")
    else:
        report.add(WARN, "calendar roster", "off: names come from titles, intros, addressing, voice profiles")


def check_sessions(report, base=None, limit=5):
    base = base or os.path.expanduser("~/meeting-notes")
    if not os.path.isdir(base):
        return
    names = sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))[-limit:]
    for name in names:
        try:
            with open(os.path.join(base, name, "status.json"), "r", encoding="utf-8") as handle:
                status = json.load(handle)
        except (OSError, ValueError):
            continue
        source = status.get("speaker_source")
        # History is reported, not failed: an old mic-only call cannot be fixed now.
        level = WARN if source == "mic-only" else PASS
        extra = " audio kept: " + str(status.get("audio_kept_reason")) if status.get("audio_kept") else ""
        report.add(level, f"session {name}", f"state={status.get('state')} speakers={source} delivered={status.get('delivered')}{extra}")


# --- entry ---------------------------------------------------------------

def run(args, config, out=None):
    report = Report(out)
    check_watcher(report)
    check_helper(report)
    check_models(report)
    check_config(report, config)
    check_sessions(report)
    wants_capture = args.capture or args.e2e or args.verify_signed
    if wants_capture:
        if recording_now():
            report.add(FAIL, "capture", "a call is being recorded right now; not running the capture test")
            return report
        helper = None
        if args.verify_signed:
            from meeting_notes.macos_capture import signed_helper_path

            helper = signed_helper_path()
            if not os.path.isfile(helper):
                report.add(FAIL, "signed helper", f"{helper} not built (run scripts/install_macos.sh)")
                return report
            from meeting_notes.responsible_spawn import run_responsible

            # Must disclaim Python/Terminal, otherwise this inherits Terminal's
            # Screen Recording grant and writes a false "granted" marker.
            code, text = run_responsible([helper, "permissions", "--request"], timeout=120)
            report.add(PASS, "signed helper permissions", (text or "").strip() or f"exit {code}")
        work = tempfile.mkdtemp(prefix="meeting-notes-selftest-")
        try:
            clip, turns = build_clip(work)
            folder = os.path.join(work, "session")
            os.makedirs(folder)
            result = capture_clip(folder, clip, helper=helper)
            print(json.dumps(result, indent=1), file=report.out)
            captured = judge_capture(result, report)
            if captured and args.verify_signed:
                _write_signed_marker(helper)
                from meeting_notes.macos_capture import retire_shim

                backup = retire_shim()
                note = "verified; recording now uses it (shim no longer loaded)"
                if backup:
                    note += f"; shim moved to {backup}"
                report.add(PASS, "signed helper", note)
            if captured and args.e2e:
                text, document = name_capture(folder, config, model=args.model)
                print("--- transcript ---", file=report.out)
                print(text, file=report.out)
                judge_names(document, report)
        finally:
            if args.keep:
                print(f"kept: {work}", file=report.out)
            else:
                shutil.rmtree(work, ignore_errors=True)
    print("SELFTEST " + ("PASSED" if report.ok else "FAILED"), file=report.out)
    return report


def _write_signed_marker(helper):
    from meeting_notes.atomic_io import atomic_write_json
    from meeting_notes.macos_capture import SIGNED_MARKER

    screen = False
    try:
        from meeting_notes.responsible_spawn import run_responsible

        _code, text = run_responsible([helper, "permissions"], timeout=30)
        screen = bool(json.loads((text or "").strip() or "{}").get("screen_recording"))
    except Exception:
        screen = False
    atomic_write_json(
        os.path.join(control_dir(), "bin", SIGNED_MARKER),
        {"record_ok": True, "screen_recording": screen, "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "helper": helper},
    )
