"""Platform dispatcher for audio recording.

Both backends expose the same two functions:

    record_audio(output_path) -> handle
    stop_recording(handle)

The "handle" is opaque to the CLI — Mac returns an ffmpeg subprocess, Windows
returns a WindowsRecorderHandle that bundles capture threads, a stop event,
and the open WAV file.
"""

import sys


if sys.platform == "darwin":
    from meeting_notes.recorder_mac import record_audio, stop_recording
elif sys.platform == "win32":
    from meeting_notes.recorder_windows import record_audio, stop_recording
else:
    raise RuntimeError(
        f"Unsupported platform: {sys.platform}. "
        "This tool runs on macOS (darwin) and Windows (win32) only."
    )


__all__ = ["record_audio", "stop_recording"]
