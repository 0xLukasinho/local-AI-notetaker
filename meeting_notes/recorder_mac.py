import os
import shutil
import subprocess
import sys
import threading

from meeting_notes.macos_capture import (
    avfoundation_device,
    capture_backend_for,
    find_capture_helper,
    helper_record_argv,
)
from meeting_notes.responsible_spawn import popen_responsible


def check_ffmpeg():
    """Verify ffmpeg is installed and available."""
    if not shutil.which("ffmpeg"):
        print("Error: ffmpeg is not installed.", file=sys.stderr)
        print("Install it with one of:", file=sys.stderr)
        print("  brew install ffmpeg", file=sys.stderr)
        print("  Or download from https://evermeet.cx/ffmpeg/", file=sys.stderr)
        sys.exit(1)


def list_audio_devices():
    """List available AVFoundation audio devices."""
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    return result.stderr


def _relay_stderr(stream):
    try:
        for raw in iter(stream.readline, b""):
            print(raw.decode("utf-8", "replace"), end="", file=sys.stderr)
    except Exception:
        pass


def _start_ffmpeg(output_path, device_index):
    check_ffmpeg()
    cmd = [
        "ffmpeg",
        "-f", "avfoundation",
        "-i", ":%s" % device_index,
        "-ar", "16000",
        "-ac", "1",
        "-y",
        output_path,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _track_paths(output_path):
    folder = os.path.dirname(os.path.abspath(output_path))
    return (
        os.path.join(folder, "audio-self.wav"),
        os.path.join(folder, "audio-others.wav"),
    )


def _start_helper(helper, output_path, device_index, separate_tracks=False):
    """Record with the Swift helper. If it exits immediately, use ffmpeg.

    The helper mixes system audio and the microphone. ffmpeg records one
    AVFoundation input and cannot hear other applications, so it is only the
    fallback when the helper is missing or refuses to start.
    """
    process = popen_responsible(
        helper_record_argv(
            helper,
            output_path,
            *(_track_paths(output_path) if separate_tracks else (None, None)),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        process.wait(timeout=0.6)
    except subprocess.TimeoutExpired:
        if process.stderr is not None:
            threading.Thread(
                target=_relay_stderr, args=(process.stderr,), daemon=True
            ).start()
        return process
    if process.stderr is not None:
        try:
            error = process.stderr.read()
        except Exception:
            error = b""
        if error:
            print(error.decode("utf-8", "replace"), end="", file=sys.stderr)
    print(
        "System-audio helper exited. Recording the AVFoundation input device "
        "instead (other participants are on that device only if it already "
        "carries their audio).",
        file=sys.stderr,
    )
    return _start_ffmpeg(output_path, device_index)


def record_audio(output_path, device_index="1", separate_tracks=False):
    """Start a macOS recording and return the subprocess handle.

    `separate_tracks` matches the Windows recorder. The helper then writes
    audio-self.wav (microphone) and audio-others.wav (system audio) beside
    the mix, so speaker names use the same tile and two-person rules as
    Windows and do not require captions.

    When ``scripts/setup_macos.sh`` has built the helper, that program
    captures system audio (Core Audio process tap, then ScreenCaptureKit)
    plus the microphone. Otherwise this is the original ffmpeg AVFoundation
    recording of one input device.
    """
    device = avfoundation_device(os.environ, device_index)
    helper = find_capture_helper()
    backend = capture_backend_for(os.environ, helper is not None)
    if backend == "helper":
        if not helper:
            print(
                "Error: MEETING_NOTES_CAPTURE=helper but the audio helper is not installed.",
                file=sys.stderr,
            )
            print("Run scripts/setup_macos.sh, or unset MEETING_NOTES_CAPTURE.", file=sys.stderr)
            sys.exit(1)
        return _start_helper(helper, output_path, device, separate_tracks=separate_tracks)
    return _start_ffmpeg(output_path, device)


def stop_recording(process):
    """Stop ffmpeg or the audio helper by sending ``q`` and closing stdin.

    ffmpeg quits on the ``q`` byte. The helper reads a line, and also stops
    when stdin hits EOF, which is what closing the pipe does.
    """
    if process.poll() is None and process.stdin:
        try:
            process.stdin.write(b"q\n")
            process.stdin.flush()
            process.stdin.close()
        except Exception:
            pass
        process.wait(timeout=10)
