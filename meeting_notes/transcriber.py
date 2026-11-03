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


def _format_rows(rows):
    lines = []
    for row in rows:
        start = row["start"]
        minutes = int(start // 60)
        seconds = int(start % 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {row['text']}")
    return "\n".join(lines)


def _collect_segments(segments, total_duration, with_words):
    rows = []
    for segment in segments:
        start = segment.start
        text = segment.text.strip()
        row = {"start": float(start), "end": float(segment.end), "text": text}
        if with_words:
            words = []
            for word in getattr(segment, "words", None) or []:
                token = getattr(word, "word", "") or ""
                token = token.strip()
                if not token:
                    continue
                words.append(
                    {
                        "start": float(word.start),
                        "end": float(word.end),
                        "text": token,
                    }
                )
            row["words"] = words
        rows.append(row)
        minutes = int(start // 60)
        seconds = int(start % 60)
        if total_duration:
            pct = min(100, int(segment.end / total_duration * 100))
            print(f"\r  transcribing... {pct:3d}%  [{minutes:02d}:{seconds:02d}]",
                  end="", flush=True)
    print()  # finish the progress line
    return rows


def _load_model(model_size, device, compute_type):
    # Imported here so CLI commands that never transcribe (list, tests) do not
    # require the faster-whisper package to be installed.
    from faster_whisper import WhisperModel

    print(f"Loading Whisper model ({model_size}) on {device} [{compute_type}]...")
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _transcribe_one(model, audio_path, language, word_timestamps, label):
    if label:
        print(f"Transcribing {label}...")
    else:
        print("Transcribing audio...")
    segments, info = model.transcribe(
        audio_path,
        language=language,
        vad_filter=True,
        word_timestamps=bool(word_timestamps),
    )
    return _collect_segments(segments, getattr(info, "duration", None), with_words=word_timestamps)


def _run_paths(paths, model_size, language, device, compute_type, word_timestamps, labels):
    model = _load_model(model_size, device, compute_type)
    rows = []
    for path, label in zip(paths, labels):
        rows.append(
            _transcribe_one(model, path, language, word_timestamps, label if len(paths) > 1 else "")
        )
    return rows


def _labels_for(paths):
    labels = []
    for path in paths:
        name = os.path.basename(path).lower()
        if "self" in name:
            labels.append("microphone (you)")
        elif "other" in name:
            labels.append("other participants")
        else:
            labels.append(os.path.basename(path))
    return labels


def transcribe_paths(paths, model_size="medium", language="en", word_timestamps=False):
    """Transcribe each path with one model load.

    Returns a list of segment lists, one per path. Each segment is
    `{start, end, text}` and, when `word_timestamps` is set, `words`.
    """
    if language in (None, "", "auto"):
        language = None
    paths = list(paths)
    if not paths:
        return []
    labels = _labels_for(paths)
    device, compute_type = _select_device()
    try:
        return _run_paths(
            paths, model_size, language, device, compute_type, word_timestamps, labels
        )
    except Exception as e:
        if device == "cuda":
            print(f"\nGPU transcription failed ({e}); falling back to CPU.",
                  file=sys.stderr)
            return _run_paths(
                paths, model_size, language, "cpu", "int8", word_timestamps, labels
            )
        raise


def _run(audio_path, model_size, language, device, compute_type):
    rows = _run_paths(
        [audio_path], model_size, language, device, compute_type, False, [""]
    )
    return _format_rows(rows[0])


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
