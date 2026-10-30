"""Windows audio recorder: mixes system audio (WASAPI loopback) + microphone.

Backend notes (hard-won — see the threading constraints below):

* Uses pyaudiowpatch (a PyAudio fork with WASAPI loopback). The previously used
  `soundcard` library corrupted the process heap (STATUS_HEAP_CORRUPTION) inside
  WASAPI ``IAudioClient::Initialize`` on this hardware and killed the process the
  instant recording started.
* PortAudio/WASAPI here is unforgiving about threading:
    - A PyAudio object must be created AND used on the same thread; using a
      stream from a thread other than the one that opened it → access violation.
    - Two ``Pa_Initialize`` / ``Pa_Terminate`` calls running concurrently →
      access violation, so every PyAudio lifetime is serialised under
      ``_INIT_LOCK``. ``Pa_Terminate`` is reference-counted: a short-lived probe
      instance must not be terminated on a thread that races another instance's
      init, but terminating it while other instances are alive only drops one
      reference and leaves their streams up. That probe is how newly plugged-in
      headphones show up; PortAudio's device list is a snapshot from init.
    - Two WASAPI streams opened on a single thread starve each other, so each
      source gets its own dedicated capture thread + its own PyAudio instance.
    - Each capture thread must initialise COM for itself (``CoInitializeEx``).
* WASAPI loopback only delivers samples while audio is actually playing on THAT
  endpoint, and the stream stays bound to the endpoint it was opened on. Chrome
  follows the Windows default output, and a per-app device (Meet's speaker
  picker, or headphones that connect after the call starts) is a different
  endpoint. Capturing only the default-at-start loopback records silence for the
  remote side as soon as playback moves. Every render loopback is captured, and
  the set is refreshed when devices appear or disappear. A loopback error does
  not stop the microphone.
* WASAPI loopback does not produce silence while nothing is playing. The
  microphone drives the timeline (it streams continuously) and system audio is
  overlaid where present. Each endpoint has its own buffer so a quiet device
  cannot shift another device's audio later in the file.
"""

import ctypes
import os
import sys
import threading
import time
import wave
from collections import deque

import numpy as np
import pyaudiowpatch as pyaudio

from meeting_notes.capture_plan import (
    MIC_ACTIVE_RMS,
    SPEAKER_ACTIVE_RMS,
    SYSTEM_SILENCE_REMIND_SECONDS,
    SYSTEM_SILENCE_WARN_SECONDS,
    AudioRoute,
    OutputEndpoint,
    SilenceTracker,
    describe_route_change,
    mark_default_loopback,
    note_output_misses,
    outputs_to_open,
    rms_from_sum_squares,
    snapshot_route,
    take_span,
)
from meeting_notes.device_log import DeviceLog


SAMPLE_RATE = 16000      # target rate written to the WAV (what Whisper expects)
TARGET_BLOCK = 1600      # 100 ms at 16 kHz — granularity the mixer writes in
READ_FRAMES = 1024       # frames pulled from a native-rate device per read
MIC_GAIN = 0.7           # keep system audio dominant in the mix
MIX_POLL = 0.01          # seconds the mixer/poller sleeps when waiting for audio
SPK_LAG_CAP = SAMPLE_RATE  # cap buffered system audio at ~1 s to bound drift
DEVICE_POLL_SECONDS = 1.0
LOOPBACK_RETRY_SECONDS = 5.0
ANALYSIS_WINDOW = SAMPLE_RATE  # 1 s of mic audio per silence check

# Serialises Pa_Initialize / Pa_Terminate across capture threads.
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
        self.reopen_loopbacks = threading.Event()
        self.threads = []
        self.wav_file = None
        self.error = None
        self._error_lock = threading.Lock()
        self._silence_lock = threading.Lock()
        self._silence_message = None

    def set_error(self, exc):
        with self._error_lock:
            if self.error is None:
                self.error = exc
        self.stop_event.set()

    def request_loopback_reopen(self, message):
        with self._silence_lock:
            self._silence_message = message
        self.reopen_loopbacks.set()

    def take_silence_message(self):
        with self._silence_lock:
            message = self._silence_message
            self._silence_message = None
        return message or (
            "system audio silent while the microphone is active; reopening output capture"
        )


class _Worker:
    def __init__(self, name):
        self.name = name
        self.stop = threading.Event()
        self.thread = None
        self.failed = False
        self.error = None
        self.opened = False
        self.stable_polls = 0

    def alive(self):
        return self.thread is not None and self.thread.is_alive()


class _SinkMap:
    """Per-endpoint queues. Append and popleft stay on separate threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sinks = {}

    def sink_for(self, name):
        with self._lock:
            sink = self._sinks.get(name)
            if sink is None:
                sink = deque()
                self._sinks[name] = sink
            return sink

    def snapshot(self):
        with self._lock:
            return list(self._sinks.items())


def _to_mono_float(raw, channels):
    """Convert interleaved int16 PCM bytes to a mono float32 array in [-1, 1]."""
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        usable = (samples.size // channels) * channels  # drop a partial frame
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32) / 32768.0


def _with_pyaudio(fn):
    """Create a PyAudio, run ``fn``, and terminate it under the init lock."""
    with _INIT_LOCK:
        pa = pyaudio.PyAudio()
        try:
            return fn(pa)
        finally:
            try:
                pa.terminate()
            except Exception:
                pass


def _default_output_name(pa):
    try:
        if hasattr(pa, "get_default_wasapi_device"):
            info = pa.get_default_wasapi_device(d_out=True)
            name = info.get("name")
            if name:
                return name
    except Exception:
        pass
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        info = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        return info.get("name")
    except Exception:
        return None


def _default_input_name(pa):
    """WASAPI default input when that API exists, else PortAudio's default."""
    try:
        if hasattr(pa, "get_default_wasapi_device"):
            info = pa.get_default_wasapi_device(d_in=True)
            if info and not info.get("isLoopbackDevice") and info.get("name"):
                if int(info.get("maxInputChannels") or 0) > 0:
                    return info.get("name")
    except Exception:
        pass
    try:
        info = pa.get_default_input_device_info()
    except Exception:
        return None
    if not info or info.get("isLoopbackDevice"):
        return None
    if int(info.get("maxInputChannels") or 0) < 1:
        return None
    return info.get("name")


def _loopback_names(pa):
    names = []
    try:
        generator = pa.get_loopback_device_info_generator()
    except Exception:
        return names
    for info in generator:
        name = info.get("name")
        if name and name not in names:
            names.append(name)
    return names


def _probe_route():
    def read(pa):
        render = _default_output_name(pa)
        names = _loopback_names(pa)
        default_name = mark_default_loopback(render, names)
        outputs = [
            OutputEndpoint(name, is_default=(name == default_name))
            for name in names
        ]
        return snapshot_route(_default_input_name(pa), outputs)

    return _with_pyaudio(read)


def _lookup_loopback(pa, name):
    for info in _iter_loopbacks(pa):
        if info.get("name") == name:
            return info
    return None


def _iter_loopbacks(pa):
    try:
        return list(pa.get_loopback_device_info_generator())
    except Exception:
        return []


def _lookup_input(pa, name):
    wasapi_index = None
    try:
        wasapi_index = pa.get_host_api_info_by_type(pyaudio.paWASAPI).get("index")
    except Exception:
        wasapi_index = None
    fallback = None
    try:
        count = int(pa.get_device_count())
    except Exception:
        return None
    for index in range(count):
        try:
            info = pa.get_device_info_by_index(index)
        except Exception:
            continue
        if info.get("name") != name or info.get("isLoopbackDevice"):
            continue
        if int(info.get("maxInputChannels") or 0) < 1:
            continue
        if wasapi_index is not None and info.get("hostApi") == wasapi_index:
            return info
        if fallback is None:
            fallback = info
    return fallback


def _capture(resolve_device, sink, handle, poll, local_stop, on_error, on_open):
    """Own a PyAudio instance + one stream on this thread; feed resampled mono
    audio into `sink`.

    ``poll`` reads only what's available (loopback, which is bursty); otherwise
    the read blocks (mic, continuous). Device errors are reported and this
    thread exits. They do not stop the rest of the recording.
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COM for this thread (MTA)
    except Exception:
        pass
    pa = None
    stream = None
    try:
        with _INIT_LOCK:  # concurrent Pa_Initialize / open() crashes WASAPI
            pa = pyaudio.PyAudio()
            dev = resolve_device(pa)
            if dev is None:
                raise RuntimeError("audio device not found")
            channels = max(1, int(dev["maxInputChannels"]))
            src_rate = int(dev["defaultSampleRate"])
            stream = pa.open(
                format=pyaudio.paInt16, channels=channels, rate=src_rate,
                frames_per_buffer=READ_FRAMES, input=True,
                input_device_index=int(dev["index"]),
            )

        if on_open is not None:
            on_open(dev, channels, src_rate)
        resampler = _LinearResampler(src_rate, SAMPLE_RATE)
        while not handle.stop_event.is_set() and not local_stop.is_set():
            if poll:
                avail = stream.get_read_available()
                if avail <= 0:
                    if handle.stop_event.wait(timeout=MIX_POLL):
                        break
                    if local_stop.is_set():
                        break
                    continue
                raw = stream.read(avail, exception_on_overflow=False)
            else:
                raw = stream.read(READ_FRAMES, exception_on_overflow=False)
            if local_stop.is_set() or handle.stop_event.is_set():
                break
            block = resampler.process(_to_mono_float(raw, channels))
            if block.size:
                sink.append(block)
    except Exception as exc:
        if on_error is not None and not local_stop.is_set() and not handle.stop_event.is_set():
            on_error(exc)
    finally:
        with _INIT_LOCK:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if pa is not None:
                    pa.terminate()
            except Exception:
                pass
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def _drain_sink(sink):
    chunks = []
    while sink:
        chunks.append(sink.popleft())
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    return np.concatenate(chunks)


def _mixer(spk_sinks, mic_sink, wav_file, handle, log):
    """Mix every loopback over the microphone timeline, writing PCM16 to the WAV.

    The microphone is the master clock (continuous). Each output endpoint keeps
    its own buffer. A block with no samples from an endpoint is silence on that
    endpoint, not a delay of the others. System audio is summed and clipped.
    """
    spk_bufs = {}
    mic_buf = np.zeros(0, dtype=np.float32)
    tracker = SilenceTracker()
    sum_sq_mic = 0.0
    sum_sq_spk = 0.0
    counted = 0
    try:
        while True:
            while mic_sink:
                mic_buf = np.concatenate((mic_buf, mic_sink.popleft()))
            for name, sink in spk_sinks.snapshot():
                extra = _drain_sink(sink)
                if extra is None:
                    continue
                prev = spk_bufs.get(name)
                if prev is None or prev.size == 0:
                    spk_bufs[name] = extra
                else:
                    spk_bufs[name] = np.concatenate((prev, extra))
            for name, buf in list(spk_bufs.items()):
                if buf.size > SPK_LAG_CAP:
                    spk_bufs[name] = buf[-SPK_LAG_CAP:]

            stopping = handle.stop_event.is_set()
            wrote = False
            while mic_buf.size >= TARGET_BLOCK or (stopping and mic_buf.size > 0):
                n = min(TARGET_BLOCK, mic_buf.size)
                mic_part = mic_buf[:n]
                mic_buf = mic_buf[n:]
                acc = np.zeros(n, dtype=np.float32)
                for name, buf in list(spk_bufs.items()):
                    take, _pad = take_span(int(buf.size), n)
                    if take:
                        acc[:take] += buf[:take]
                        spk_bufs[name] = buf[take:]
                mix = acc + MIC_GAIN * mic_part
                np.clip(mix, -1.0, 1.0, out=mix)
                wav_file.writeframes((mix * 32767.0).astype(np.int16).tobytes())
                wrote = True
                sum_sq_mic += float(np.dot(mic_part, mic_part))
                sum_sq_spk += float(np.dot(acc, acc))
                counted += n
                if counted >= ANALYSIS_WINDOW:
                    window_s = counted / float(SAMPLE_RATE)
                    mic_rms = rms_from_sum_squares(sum_sq_mic, counted)
                    spk_rms = rms_from_sum_squares(sum_sq_spk, counted)
                    decision = tracker.observe_window(mic_rms, spk_rms, window_s)
                    sum_sq_mic = 0.0
                    sum_sq_spk = 0.0
                    counted = 0
                    _note_silence(handle, log, decision, mic_rms, spk_rms)

            if stopping and not _sink_pending(mic_sink) and mic_buf.size == 0:
                break
            if not wrote:
                handle.stop_event.wait(timeout=MIX_POLL)
    except Exception as exc:
        handle.set_error(exc)


def _sink_pending(sink):
    return bool(sink)


def _note_silence(handle, log, decision, mic_rms, spk_rms):
    if decision == "recovered":
        log.event(
            "system audio resumed "
            f"(mic rms {mic_rms:.4f}, system rms {spk_rms:.4f})"
        )
        return
    if decision not in ("warn", "remind"):
        return
    seconds = int(SYSTEM_SILENCE_WARN_SECONDS if decision == "warn" else SYSTEM_SILENCE_REMIND_SECONDS)
    if decision == "warn":
        message = (
            f"system audio silent for {seconds}s while the microphone is active "
            f"(mic rms {mic_rms:.4f}, system rms {spk_rms:.4f}, "
            f"thresholds mic>={MIC_ACTIVE_RMS}, system>={SPEAKER_ACTIVE_RMS}); "
            "reopening output capture"
        )
    else:
        message = (
            "system audio still silent while the microphone is active "
            f"(mic rms {mic_rms:.4f}, system rms {spk_rms:.4f}); reopening output capture"
        )
    handle.request_loopback_reopen(message)


def _start_worker(name, resolve, sink, handle, log, poll, on_open):
    worker = _Worker(name)

    def on_error(exc):
        worker.failed = True
        worker.error = exc
        worker.stable_polls = 0

    def opened(dev, channels, rate):
        worker.opened = True
        if on_open is not None:
            on_open(dev, channels, rate)

    worker.thread = threading.Thread(
        target=_capture,
        args=(resolve, sink, handle, poll, worker.stop, on_error, opened),
        daemon=True,
        name=f"capture-{name}",
    )
    worker.thread.start()
    return worker


def _join_worker(worker, timeout):
    thread = worker.thread
    if thread is None:
        return
    thread.join(timeout=timeout)


def _supervisor(handle, spk_sinks, mic_sink, log):
    """Open a loopback per render endpoint and follow default-device changes."""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)
    except Exception:
        pass
    outputs = {}
    misses = {}
    cooldown_until = {}
    mic_worker = None
    mic_name = None
    previous = None
    empty_warned = False
    scan_error = None
    # Devices that fail to open (a disconnected HDMI port, for example) are
    # retried quietly after the first error so a long call does not fill the log.
    reported = set()
    mic_fault_logged = {"value": False}
    mic_retry_at = 0.0
    try:
        while not handle.stop_event.is_set():
            if handle.reopen_loopbacks.is_set():
                handle.reopen_loopbacks.clear()
                names = ", ".join(sorted(outputs)) or "(none)"
                log.event(f"{handle.take_silence_message()}. open outputs: {names}")
                cooldown_until = {}
                reported.clear()
                for worker in outputs.values():
                    worker.stop.set()
                for name, worker in list(outputs.items()):
                    _join_worker(worker, 1.0)
                    if not worker.alive():
                        outputs.pop(name, None)
                misses = {}

            try:
                route = _probe_route()
                scan_error = None
            except Exception as exc:
                text = str(exc)
                if text != scan_error:
                    log.event(f"audio device scan failed: {text}")
                    scan_error = text
                if handle.stop_event.wait(DEVICE_POLL_SECONDS):
                    break
                continue

            if not route.outputs and previous is not None and previous.outputs:
                if not empty_warned:
                    log.event(
                        "output scan returned no loopback devices; "
                        "keeping streams already open"
                    )
                    empty_warned = True
                route = AudioRoute(route.microphone, previous.outputs)
            else:
                empty_warned = False

            for line in describe_route_change(previous, route):
                log.event(line)
            previous = route

            for name, worker in list(outputs.items()):
                if worker.alive():
                    if worker.opened and name in reported:
                        worker.stable_polls += 1
                        if worker.stable_polls >= 2:
                            reported.discard(name)
                            log.event(f"loopback '{name}' is capturing again")
                    continue
                outputs.pop(name, None)
                if worker.failed:
                    cooldown_until[name] = time.monotonic() + LOOPBACK_RETRY_SECONDS
                    if name not in reported:
                        detail = f": {worker.error}" if worker.error else ""
                        log.event(
                            f"loopback '{name}' capture error{detail}; "
                            f"retrying every {int(LOOPBACK_RETRY_SECONDS)}s"
                        )
                        reported.add(name)
                else:
                    log.event(f"loopback capture ended for '{name}'")

            wanted = [endpoint.name for endpoint in route.outputs]
            misses, stop_names = note_output_misses(misses, list(outputs), wanted)
            for name in stop_names:
                worker = outputs.get(name)
                if worker is None:
                    continue
                worker.stop.set()
                _join_worker(worker, 1.0)
                if worker.alive():
                    misses[name] = 2
                    continue
                outputs.pop(name, None)
                log.event(f"stopped loopback '{name}'")

            now = time.monotonic()
            roles = {endpoint.name: endpoint.is_default for endpoint in route.outputs}
            for name in outputs_to_open(list(outputs), wanted):
                if cooldown_until.get(name, 0) > now:
                    continue
                is_default = roles.get(name, False)

                def resolve(pa, device_name=name):
                    return _lookup_loopback(pa, device_name)

                def on_open(dev, channels, rate, device_name=name, default=is_default):
                    # A retry after a logged failure stays quiet until the stream
                    # survives a poll. The first open, and any later reopen that
                    # is not a retry of that failure, is logged.
                    if device_name in reported:
                        return
                    role = "default output" if default else "non-default output"
                    log.event(
                        f"opened loopback '{device_name}' ({rate} Hz, {channels} ch, {role})"
                    )

                outputs[name] = _start_worker(
                    name,
                    resolve,
                    spk_sinks.sink_for(name),
                    handle,
                    log,
                    True,
                    on_open,
                )

            if (
                mic_worker is not None
                and mic_worker.alive()
                and mic_worker.opened
                and mic_fault_logged["value"]
            ):
                mic_worker.stable_polls += 1
                if mic_worker.stable_polls >= 2:
                    mic_fault_logged["value"] = False
                    log.event(f"microphone '{mic_name}' is capturing again")

            if route.microphone:
                mic_changed = mic_name != route.microphone
                mic_dead = mic_worker is not None and not mic_worker.alive()
                if mic_worker is not None and (mic_changed or mic_dead):
                    if mic_worker.alive():
                        mic_worker.stop.set()
                        _join_worker(mic_worker, 1.0)
                    if mic_worker.alive():
                        log.event(
                            f"microphone '{mic_name}' is stuck; "
                            f"not opening '{route.microphone}' yet"
                        )
                    else:
                        if mic_dead and not mic_changed:
                            if not mic_fault_logged["value"]:
                                detail = f": {mic_worker.error}" if mic_worker.error else ""
                                log.event(
                                    f"microphone capture ended for '{mic_name}'{detail}; "
                                    f"retrying every {int(LOOPBACK_RETRY_SECONDS)}s"
                                )
                                mic_fault_logged["value"] = True
                            mic_retry_at = time.monotonic() + LOOPBACK_RETRY_SECONDS
                        if mic_changed:
                            mic_fault_logged["value"] = False
                            mic_retry_at = 0.0
                        mic_worker = None
                if mic_worker is None and time.monotonic() >= mic_retry_at:
                    chosen = route.microphone

                    def resolve_mic(pa, device_name=chosen):
                        return _lookup_input(pa, device_name)

                    def on_open_mic(dev, channels, rate, device_name=chosen):
                        if mic_fault_logged["value"]:
                            return
                        log.event(
                            f"opened microphone '{device_name}' ({rate} Hz, {channels} ch)"
                        )

                    mic_name = chosen
                    mic_worker = _start_worker(
                        chosen, resolve_mic, mic_sink, handle, log, False, on_open_mic,
                    )

            if handle.stop_event.wait(DEVICE_POLL_SECONDS):
                break
    finally:
        for worker in list(outputs.values()):
            worker.stop.set()
        if mic_worker is not None:
            mic_worker.stop.set()
        for worker in list(outputs.values()):
            _join_worker(worker, 1.0)
        if mic_worker is not None:
            _join_worker(mic_worker, 1.0)
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def _fail_startup(message, hint):
    print(message, file=sys.stderr)
    print(hint, file=sys.stderr)
    sys.exit(1)


def record_audio(output_path):
    """Start a background capture of system audio (every loopback) + microphone.

    Returns a WindowsRecorderHandle. Recording continues until stop_recording()
    is called. Output devices that appear or become default while recording are
    picked up; disappearing devices are dropped without stopping the take.
    """
    # Probe devices up front (on the calling thread) so we can fail fast with a
    # clear message before opening the WAV / spawning threads.
    try:
        route = _probe_route()
    except Exception as exc:
        _fail_startup(
            f"Error: Could not query audio devices ({exc}).",
            "Check that Windows audio is running and a playback device is enabled.",
        )
    if not isinstance(route, AudioRoute) or not route.outputs:
        _fail_startup(
            "Error: No WASAPI loopback device found.",
            "Set a playback device in Windows Sound settings "
            "(Settings -> System -> Sound).",
        )
    if not route.microphone:
        _fail_startup(
            "Error: No default microphone configured.",
            "Set a default input device in Windows Sound settings "
            "(Settings -> System -> Sound).",
        )

    wav_file = wave.open(output_path, "wb")
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(SAMPLE_RATE)

    handle = WindowsRecorderHandle()
    handle.wav_file = wav_file

    spk_sinks = _SinkMap()
    mic_sink = deque()
    log = DeviceLog(os.path.dirname(os.path.abspath(output_path)))

    threads = [
        threading.Thread(
            target=_supervisor,
            args=(handle, spk_sinks, mic_sink, log),
            daemon=True,
            name="audio-devices",
        ),
        threading.Thread(
            target=_mixer,
            args=(spk_sinks, mic_sink, wav_file, handle, log),
            daemon=True,
            name="mixer",
        ),
    ]
    for thread in threads:
        thread.start()
    handle.threads = threads
    return handle


def stop_recording(handle):
    """Signal all threads to stop, join them, close the WAV file.

    Re-raises any exception that happened in a worker thread. A loopback that
    failed mid-call is not one of those: the microphone keeps recording, and
    the device supervisor logs the failure and retries.
    """
    handle.stop_event.set()
    for thread in handle.threads:
        thread.join(timeout=15)
    if handle.wav_file is not None:
        try:
            handle.wav_file.close()
        except Exception:
            pass
        handle.wav_file = None
    if handle.error is not None:
        raise handle.error
