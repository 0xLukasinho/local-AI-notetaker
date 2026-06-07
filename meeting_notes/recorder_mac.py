import shutil
import subprocess
import sys


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


def record_audio(output_path, device_index="1"):
    """Start recording audio via ffmpeg AVFoundation.

    Returns the running ffmpeg subprocess.Popen, which is treated as an
    opaque "handle" by the CLI.
    """
    check_ffmpeg()

    cmd = [
        "ffmpeg",
        "-f", "avfoundation",
        "-i", f":{device_index}",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        output_path,
    ]

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return process


def stop_recording(process):
    """Gracefully stop ffmpeg recording by sending 'q' to its stdin."""
    if process.poll() is None:
        process.stdin.write(b"q")
        process.stdin.flush()
        process.wait(timeout=10)
