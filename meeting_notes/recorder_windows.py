"""Windows audio recorder: mixes system audio (WASAPI loopback) + microphone.

Backend notes (hard-won — see the threading constraints below):

* Uses pyaudiowpatch (a PyAudio fork with WASAPI loopback). The previously used
  `soundcard` library corrupted the process heap (STATUS_HEAP_CORRUPTION) inside
  WASAPI ``IAudioClient::Initialize`` on this hardware and killed the process the
  instant recording started.
* PortAudio/WASAPI here is unforgiving about threading:
    - A PyAudio object must be created AND used on the same thread; using a
      stream from a thread other than the one that opened it → access violation.
    - Two ``Pa_Initialize`` calls running concurrently → access violation, so
      PyAudio construction is serialised under ``_INIT_LOCK``.
    - Two WASAPI streams opened on a single thread starve each other, so each
      source gets its own dedicated capture thread + its own PyAudio instance.
    - Each capture thread must initialise COM for itself (``CoInitializeEx``).
* WASAPI loopback only delivers samples while audio is actually playing; it does
  not produce silence. The microphone therefore drives the timeline (it streams
  continuously) and system audio is overlaid where present.
"""

import ctypes
import sys
import threading
import wave
from collections import deque

import numpy as np
import pyaudiowpatch as pyaudio


SAMPLE_RATE = 16000      # target rate written to the WAV (what Whisper expects)
TARGET_BLOCK = 1600      # 100 ms at 16 kHz — granularity the mixer writes in
READ_FRAMES = 1024       # frames pulled from a native-rate device per read
MIC_GAIN = 0.7           # keep system audio dominant in the mix
MIX_POLL = 0.01          # seconds the mixer/poller sleeps when waiting for audio
SPK_LAG_CAP = SAMPLE_RATE  # cap buffered system audio at ~1 s to bound drift

# Serialises Pa_Initialize across capture threads (concurrent init crashes).
_INIT_LOCK = threading.Lock()


class _LinearResampler:
    """Streaming linear resampler from src_rate to dst_rate (mono float32).

    Carries the trailing sample and a fractional read position across chunks so
    successive blocks join without clicks. Good enough for speech / Whisper.
    """

    def __init__(self, src_rate, dst_rate):
        self.step = float(src_rate) / float(dst_rate)  # input samples per output sample
        self.pos = 0.0   # coordinate (in input-sample units) of the next output sample
        self.prev = 0.0  # last input sample of the previous chunk (coordinate -1)

    def process(self, x):
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        # Coordinates -1 .. len(x)-1, with the carried sample sitting at -1.
        xp = np.arange(-1, x.size, dtype=np.float64)
        fp = np.empty(x.size + 1, dtype=np.float32)
        fp[0] = self.prev
        fp[1:] = x

        last_coord = x.size - 1
        n_out = int(np.floor((last_coord - self.pos) / self.step)) + 1
        if n_out <= 0:
            # Not enough new input for even one output sample; shift origin, wait.
            self.pos -= x.size
            self.prev = float(x[-1])
            return np.zeros(0, dtype=np.float32)

        coords = self.pos + self.step * np.arange(n_out)
        out = np.interp(coords, xp, fp).astype(np.float32)

        # Re-base the next output coordinate relative to the upcoming chunk.
        self.pos = self.pos + self.step * n_out - x.size
        self.prev = float(x[-1])
        return out


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


def _to_mono_float(raw, channels):
    """Convert interleaved int16 PCM bytes to a mono float32 array in [-1, 1]."""
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        usable = (samples.size // channels) * channels  # drop a partial frame
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32) / 32768.0


def _find_loopback(pa):
    """Return the WASAPI loopback device for the default speaker, or None."""
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None
    default_speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    name = default_speakers["name"]
    for lb in pa.get_loopback_device_info_generator():
        if name in lb["name"]:
            return lb
    return None


def _capture(pick_device, sink, handle, poll):
    """Own a PyAudio instance + one stream on this thread; feed resampled mono
    audio into `sink`.

    pick_device(pa) -> device_info. If `poll`, read only what's available
    (loopback, which is bursty); otherwise read blocking (mic, continuous).
    """
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COM for this thread (MTA)
    pa = None
    stream = None
    try:
        with _INIT_LOCK:  # concurrent Pa_Initialize / open() crashes WASAPI
            pa = pyaudio.PyAudio()
            dev = pick_device(pa)
            if dev is None:
                raise RuntimeError("audio device not found")
            channels = max(1, int(dev["maxInputChannels"]))
            src_rate = int(dev["defaultSampleRate"])
            stream = pa.open(
                format=pyaudio.paInt16, channels=channels, rate=src_rate,
                frames_per_buffer=READ_FRAMES, input=True,
                input_device_index=int(dev["index"]),
            )

        resampler = _LinearResampler(src_rate, SAMPLE_RATE)
        while not handle.stop_event.is_set():
            if poll:
                avail = stream.get_read_available()
                if avail <= 0:
                    handle.stop_event.wait(timeout=MIX_POLL)
                    continue
                raw = stream.read(avail, exception_on_overflow=False)
            else:
                raw = stream.read(READ_FRAMES, exception_on_overflow=False)
            block = resampler.process(_to_mono_float(raw, channels))
            if block.size:
                sink.append(block)
    except Exception as e:
        handle.set_error(e)
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            if pa is not None:
                pa.terminate()
        except Exception:
            pass
        ctypes.windll.ole32.CoUninitialize()


def _mixer(spk_sink, mic_sink, wav_file, handle):
    """Mix system audio over the microphone timeline, writing PCM16 to the WAV.

    The microphone is the master clock (continuous); system audio (bursty, only
    present while sound plays) is overlaid and padded with silence when absent.
    """
    spk_buf = np.zeros(0, dtype=np.float32)
    mic_buf = np.zeros(0, dtype=np.float32)
    try:
        while True:
            while mic_sink:
                mic_buf = np.concatenate((mic_buf, mic_sink.popleft()))
            while spk_sink:
                spk_buf = np.concatenate((spk_buf, spk_sink.popleft()))
            if spk_buf.size > SPK_LAG_CAP:  # bound drift if system audio runs ahead
                spk_buf = spk_buf[-SPK_LAG_CAP:]

            stopping = handle.stop_event.is_set()
            wrote = False
            while mic_buf.size >= TARGET_BLOCK or (stopping and mic_buf.size > 0):
                n = min(TARGET_BLOCK, mic_buf.size)
                mic_part = mic_buf[:n]
                if spk_buf.size >= n:
                    spk_part = spk_buf[:n]
                    spk_buf = spk_buf[n:]
                else:
                    spk_part = np.zeros(n, dtype=np.float32)
                    spk_part[:spk_buf.size] = spk_buf
                    spk_buf = np.zeros(0, dtype=np.float32)

                mix = spk_part + MIC_GAIN * mic_part
                np.clip(mix, -1.0, 1.0, out=mix)
                wav_file.writeframes((mix * 32767.0).astype(np.int16).tobytes())
                mic_buf = mic_buf[n:]
                wrote = True

            if stopping and not mic_sink and not spk_sink and mic_buf.size == 0:
                break
            if not wrote:
                handle.stop_event.wait(timeout=MIX_POLL)
    except Exception as e:
        handle.set_error(e)


def record_audio(output_path):
    """Start a background capture of system audio (loopback) + microphone.

    Returns a WindowsRecorderHandle. Recording continues until stop_recording()
    is called.
    """
    # Probe devices up front (on the main thread) so we can fail fast with a
    # clear message before opening the WAV / spawning threads.
    probe = pyaudio.PyAudio()
    try:
        if _find_loopback(probe) is None:
            print(
                "Error: No WASAPI loopback device found for the default speaker.",
                file=sys.stderr,
            )
            print(
                "Set a default playback device in Windows Sound settings "
                "(Settings -> System -> Sound).",
                file=sys.stderr,
            )
            probe.terminate()
            sys.exit(1)
        try:
            probe.get_default_input_device_info()
        except OSError:
            print("Error: No default microphone configured.", file=sys.stderr)
            print(
                "Set a default input device in Windows Sound settings "
                "(Settings -> System -> Sound).",
                file=sys.stderr,
            )
            probe.terminate()
            sys.exit(1)
    finally:
        probe.terminate()

    wav_file = wave.open(output_path, "wb")
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(SAMPLE_RATE)

    handle = WindowsRecorderHandle()
    handle.wav_file = wav_file

    spk_sink = deque()
    mic_sink = deque()

    threads = [
        threading.Thread(
            target=_capture, args=(_find_loopback, spk_sink, handle, True),
            daemon=True, name="capture-speaker",
        ),
        threading.Thread(
            target=_capture,
            args=(lambda pa: pa.get_default_input_device_info(), mic_sink, handle, False),
            daemon=True, name="capture-mic",
        ),
        threading.Thread(
            target=_mixer, args=(spk_sink, mic_sink, wav_file, handle),
            daemon=True, name="mixer",
        ),
    ]
    for t in threads:
        t.start()
    handle.threads = threads

    return handle


def stop_recording(handle):
    """Signal all threads to stop, join them, close the WAV file.

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
