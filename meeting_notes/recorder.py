"""Platform dispatcher for audio recording.

Both backends expose the same two functions:

    record_audio(output_path) -> handle
    stop_recording(handle)

The "handle" is opaque to the CLI — Mac returns an ffmpeg subprocess, Windows
returns a WindowsRecorderHandle that bundles capture threads, a stop event,
and the open WAV file.
"""

import sys


def _backend():
    """Load the platform recorder on first use.

    Import is deferred so non-recording commands and unit tests can import the
    package on Linux, where neither audio backend is installed. Calling
    record_audio / stop_recording on an unsupported platform still fails.
    """
    if sys.platform == "darwin":
        from meeting_notes import recorder_mac as backend
    elif sys.platform == "win32":
        from meeting_notes import recorder_windows as backend
    else:
        raise RuntimeError(
            f"Unsupported platform: {sys.platform}. "
            "This tool runs on macOS (darwin) and Windows (win32) only."
        )
    return backend


def record_audio(output_path):
    return _backend().record_audio(output_path)


def stop_recording(handle):
    return _backend().stop_recording(handle)


__all__ = ["record_audio", "stop_recording"]
