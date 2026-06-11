"""Audio transcription via faster-whisper (CTranslate2).

Replaces the original openai-whisper backend. Key differences / fixes:

* **Language is forced** (default English). The previous backend auto-detected
  language from the first 30 s of audio; when a recording opens with silence,
  detection latches onto a wrong language (we saw Welsh / Norwegian) and then
  transcribes the entire English meeting phonetically into gibberish. Forcing
  the language removes that failure mode. Pass language="auto" to opt back into
  detection.
* **VAD filtering** trims silence, so a quiet intro can never hijack the result
  and short pauses don't waste compute.
* **GPU auto-detection** with a CPU fallback: uses CUDA (float16) when an NVIDIA
  GPU + CUDA libraries are present, otherwise CPU (int8, the fastest CPU mode).
"""

import glob
import os
import sys

from faster_whisper import WhisperModel

_CUDA_DLLS_ADDED = False


def _enable_cuda_dlls():
    """Add pip-installed NVIDIA CUDA library dirs to the Windows DLL search path.

    The nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels drop their DLLs under
    site-packages/nvidia/<lib>/bin, which CTranslate2 won't find on Windows
    unless they're on the DLL search path. No-op on non-Windows or if the
    wheels aren't installed (CPU-only setups).
    """
    global _CUDA_DLLS_ADDED
    if _CUDA_DLLS_ADDED or sys.platform != "win32":
        return
    try:
        import nvidia  # namespace package provided by the nvidia-*-cu12 wheels
    except ImportError:
        return
    for base in nvidia.__path__:
        for bindir in glob.glob(os.path.join(base, "*", "bin")):
            if os.path.isdir(bindir):
                try:
                    os.add_dll_directory(bindir)
                except OSError:
                    pass
    _CUDA_DLLS_ADDED = True


def _select_device():
    """Return (device, compute_type): CUDA if usable, else fastest CPU mode."""
    _enable_cuda_dlls()
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _format_segments(segments, total_duration):
    """Consume the segment generator into [MM:SS] lines, printing progress."""
    lines = []
    for segment in segments:
        start = segment.start
        minutes = int(start // 60)
        seconds = int(start % 60)
        text = segment.text.strip()
        lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")

        if total_duration:
            pct = min(100, int(segment.end / total_duration * 100))
            print(f"\r  transcribing... {pct:3d}%  [{minutes:02d}:{seconds:02d}]",
                  end="", flush=True)
    print()  # finish the progress line
    return "\n".join(lines)


def _run(audio_path, model_size, language, device, compute_type):
    print(f"Loading Whisper model ({model_size}) on {device} [{compute_type}]...")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    print("Transcribing audio...")
    segments, info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,
    )
    return _format_segments(segments, getattr(info, "duration", None))


def transcribe(audio_path, model_size="medium", language="en"):
    """Transcribe an audio file using faster-whisper.

    Args:
        audio_path: Path to the audio file.
        model_size: Whisper model size (tiny, base, small, medium, large).
        language: ISO language code to force (default "en"). Use "auto" or None
            to let Whisper detect the language.

    Returns:
        str: The transcribed text as "[MM:SS] line" segments.
    """
    if language in (None, "", "auto"):
        language = None

    device, compute_type = _select_device()
    try:
        return _run(audio_path, model_size, language, device, compute_type)
    except Exception as e:
        if device == "cuda":
            print(f"\nGPU transcription failed ({e}); falling back to CPU.",
                  file=sys.stderr)
            return _run(audio_path, model_size, language, "cpu", "int8")
        raise
