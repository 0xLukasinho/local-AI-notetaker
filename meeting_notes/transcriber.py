import warnings

import whisper


def transcribe(audio_path, model_size="medium"):
    """Transcribe an audio file using Whisper.

    Args:
        audio_path: Path to the audio file.
        model_size: Whisper model size (tiny, base, small, medium, large).

    Returns:
        str: The transcribed text.
    """
    print(f"Loading Whisper model ({model_size})...")
    model = whisper.load_model(model_size)

    print("Transcribing audio (this may take a few minutes)...")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")
        result = model.transcribe(audio_path, fp16=False)

    lines = []
    for segment in result["segments"]:
        start = segment["start"]
        minutes = int(start // 60)
        seconds = int(start % 60)
        text = segment["text"].strip()
        lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")

    return "\n".join(lines)
