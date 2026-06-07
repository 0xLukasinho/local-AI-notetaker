import queue
import sys
import threading
import wave

import numpy as np
import soundcard as sc


SAMPLE_RATE = 16000
BLOCK_SIZE = 1600  # 100 ms at 16 kHz
MIC_GAIN = 0.7     # keep system audio dominant
QUEUE_MAX = 50     # ~5 s of buffered audio per source
QUEUE_TIMEOUT = 0.5


class WindowsRecorderHandle:
    """Opaque handle returned by record_audio() and consumed by stop_recording()."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.threads = []
        self.wav_file = None
        self.error = None
        self._error_lock = threading.Lock()

    def set_error(self, exc):
        with self._error_lock:
            if self.error is None:
                self.error = exc
        self.stop_event.set()


def _capture_source(rec_factory, out_queue, handle):
    """Continuously read from a soundcard recorder and push blocks onto a queue."""
    try:
        with rec_factory() as rec:
            while not handle.stop_event.is_set():
                block = rec.record(numframes=BLOCK_SIZE)
                try:
                    out_queue.put(block, timeout=QUEUE_TIMEOUT)
                except queue.Full:
                    # Mixer is behind; drop this block rather than block forever.
                    pass
    except Exception as e:
        handle.set_error(e)


def _mixer(spk_queue, mic_queue, wav_file, handle):
    """Pull paired blocks from both queues, mix, write PCM16 to the WAV."""
    try:
        while not handle.stop_event.is_set():
            try:
                spk = spk_queue.get(timeout=QUEUE_TIMEOUT)
                mic = mic_queue.get(timeout=QUEUE_TIMEOUT)
            except queue.Empty:
                continue

            spk_mono = spk[:, 0] if spk.ndim > 1 else spk
            mic_mono = mic[:, 0] if mic.ndim > 1 else mic

            n = min(len(spk_mono), len(mic_mono))
            if n == 0:
                continue

            mix = spk_mono[:n] + MIC_GAIN * mic_mono[:n]
            np.clip(mix, -1.0, 1.0, out=mix)
            pcm = (mix * 32767.0).astype(np.int16)
            wav_file.writeframes(pcm.tobytes())
    except Exception as e:
        handle.set_error(e)


def record_audio(output_path):
    """Start a background capture of system audio (loopback) + microphone.

    Returns a WindowsRecorderHandle. Recording continues until stop_recording()
    is called.
    """
    try:
        speaker = sc.default_speaker()
        mic_device = sc.default_microphone()
    except Exception as e:
        print(f"Error accessing audio devices: {e}", file=sys.stderr)
        sys.exit(1)

    if speaker is None or mic_device is None:
        print("Error: No default speaker or microphone configured.", file=sys.stderr)
        print(
            "Set defaults in Windows Sound settings (Settings -> System -> Sound).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    except Exception as e:
        print(f"Error opening speaker loopback: {e}", file=sys.stderr)
        sys.exit(1)

    wav_file = wave.open(output_path, "wb")
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(SAMPLE_RATE)

    handle = WindowsRecorderHandle()
    handle.wav_file = wav_file

    spk_queue = queue.Queue(maxsize=QUEUE_MAX)
    mic_queue = queue.Queue(maxsize=QUEUE_MAX)

    def spk_factory():
        return loopback.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK_SIZE)

    def mic_factory():
        return mic_device.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK_SIZE)

    threads = [
        threading.Thread(
            target=_capture_source, args=(spk_factory, spk_queue, handle), daemon=True,
            name="capture-speaker",
        ),
        threading.Thread(
            target=_capture_source, args=(mic_factory, mic_queue, handle), daemon=True,
            name="capture-mic",
        ),
        threading.Thread(
            target=_mixer, args=(spk_queue, mic_queue, wav_file, handle), daemon=True,
            name="mixer",
        ),
    ]
    for t in threads:
        t.start()
    handle.threads = threads

    return handle


def stop_recording(handle):
    """Signal all capture threads to stop, join them, close the WAV file.

    Re-raises any exception that happened in a worker thread.
    """
    handle.stop_event.set()
    for t in handle.threads:
        t.join(timeout=5)
    if handle.wav_file is not None:
        try:
            handle.wav_file.close()
        except Exception:
            pass
        handle.wav_file = None
    if handle.error is not None:
        raise handle.error
