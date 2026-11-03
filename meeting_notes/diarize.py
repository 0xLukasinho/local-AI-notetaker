"""Split the remote (system-audio) track into distinct voices, on this machine.

Google Meet plays everyone else as one mix. This module does not call a cloud
diarization API. It turns that WAV into time ranges tagged with a cluster id
(`"0"`, `"1"`, ...). Naming those clusters is `speakers.py`'s job.

Backends, first match wins when `backend` is `auto`:

- `pyannote` — only if that package is installed and a Hugging Face token is
  set (`hf_token`, or `HF_TOKEN` / `HUGGING_FACE_HUB_TOKEN`). The model is
  gated; the first run downloads it.
- `speechbrain` — ECAPA speaker embeddings, if the package is installed. The
  first run downloads the public VoxCeleb model into `~/.meeting-notes/models`.
- `resemblyzer` — smaller CPU embedding model, if installed. It downloads its
  weights on first use.
- `builtin` — mel-spectrogram clustering with numpy. No download. This is the
  default when nothing heavier is installed. It separates voices whose
  spectral shape differs and keeps one voice as one cluster; very similar
  voices can still split into an extra unnamed cluster rather than be merged.

Any failure returns segments=[] and a short `reason`. Callers then keep the
Meet caption / tile labels. Tokens are never included in that reason.
"""

import math
import os


# Cosine distance above this means "these windows are not the same voice",
# even when the dendrogram has no small merges to compare against. Neural
# embeddings of one person usually sit well below it; orthogonal vectors sit
# at 1.0.
_FAR_DISTANCE = 0.55
_DIRECT_CLUSTER_LIMIT = 280


def diarize_wav(path, backend="auto", hf_token="", max_speakers=8):
    """Return `{backend, segments, reason}`.

    `segments` is a list of `{start, end, cluster}`. Empty segments means the
    caller should not trust a voice split. `backend` is None in that case.
    """
    requested = (backend or "auto").strip().lower()
    if requested in ("off", "none", "disabled", "false"):
        return {"backend": None, "segments": [], "reason": "disabled"}
    token = _resolve_token(hf_token)
    try:
        cap = int(max_speakers)
    except (TypeError, ValueError):
        cap = 8
    if cap < 1:
        cap = 8
    order = _backend_order(requested, token)
    reasons = []
    for name in order:
        try:
            segments = _run_backend(name, path, token, cap)
        except Exception as exc:
            reasons.append(_safe_reason(name, exc, token))
            continue
        cleaned = _clean_segments(segments)
        if cleaned:
            return {"backend": name, "segments": cleaned, "reason": ""}
        reasons.append(f"{name} found no distinct voiced ranges")
    if not reasons:
        reasons.append("no diarization backend available")
    return {"backend": None, "segments": [], "reason": "; ".join(reasons)}


def cluster_windows(windows, max_speakers=8):
    """Cluster `{start, end, vector}` windows into voice segments.

    Vectors are compared by cosine distance. Same-voice windows are merged.
    A merge that jumps away from the within-voice distances is refused, so two
    different voices stay in different clusters even when `max_speakers` is
    lower. `max_speakers` is a hint for callers; it does not glue distant
    voices together.
    """
    del max_speakers  # kept for call-site compatibility; see docstring
    clean = []
    for item in windows or []:
        if not isinstance(item, dict):
            continue
        vector = item.get("vector")
        if not vector:
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
            values = [float(value) for value in vector]
        except (TypeError, ValueError, KeyError):
            continue
        if end < start:
            continue
        clean.append({"start": start, "end": end, "vector": values})
    clean.sort(key=lambda item: (item["start"], item["end"]))
    if not clean:
        return []
    if len(clean) == 1:
        return [_segment(clean[0]["start"], clean[0]["end"], "0")]
    if len(clean) > _DIRECT_CLUSTER_LIMIT:
        centroids, groups = _leader_compress(clean)
        labels = _labels_for_many(centroids)
        point_labels = [0] * len(clean)
        for label, group in zip(labels, groups):
            for index in group:
                point_labels[index] = label
    else:
        point_labels = _agglomerative_labels([item["vector"] for item in clean])
    return _labels_to_segments(clean, point_labels)


def _backend_order(requested, token):
    if requested == "auto":
        order = []
        if token:
            order.append("pyannote")
        order.extend(["speechbrain", "resemblyzer", "builtin"])
        return order
    if requested in ("pyannote", "speechbrain", "resemblyzer", "builtin"):
        return [requested]
    return ["builtin"]


def _run_backend(name, path, token, max_speakers):
    if name == "builtin":
        return _builtin_segments(path, max_speakers)
    if name == "resemblyzer":
        return _resemblyzer_segments(path, max_speakers)
    if name == "speechbrain":
        return _speechbrain_segments(path, max_speakers)
    if name == "pyannote":
        return _pyannote_segments(path, token, max_speakers)
    raise RuntimeError(f"unknown diarization backend {name}")


def _resolve_token(hf_token):
    token = str(hf_token or "").strip()
    if token:
        return token
    return (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip()


def _safe_reason(name, exc, token):
    text = str(exc).strip() or exc.__class__.__name__
    if token and token in text:
        text = text.replace(token, "***")
    text = " ".join(text.split())
    if len(text) > 180:
        text = text[:180] + "..."
    return f"{name} failed ({text})"


def _clean_segments(segments):
    cleaned = []
    if not segments:
        return cleaned
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (TypeError, ValueError, KeyError):
            continue
        cluster = str(item.get("cluster") if item.get("cluster") is not None else "")
        if not cluster or end <= start:
            continue
        cleaned.append({"start": start, "end": end, "cluster": cluster})
    cleaned.sort(key=lambda item: (item["start"], item["end"], item["cluster"]))
    return _renumber(cleaned)


def _renumber(segments):
    order = []
    for segment in segments:
        if segment["cluster"] not in order:
            order.append(segment["cluster"])
    mapping = {old: str(index) for index, old in enumerate(order)}
    for segment in segments:
        segment["cluster"] = mapping[segment["cluster"]]
    return segments


def _segment(start, end, cluster):
    return {"start": float(start), "end": float(end), "cluster": str(cluster)}


def _normalize(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    if norm < 1e-12:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]


def _cosine_distance(left, right):
    if not any(left) or not any(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right))
    if dot > 1.0:
        dot = 1.0
    elif dot < -1.0:
        dot = -1.0
    return 1.0 - dot


def _merges_to_keep(distances):
    """How many average-linkage merges stay inside one voice.

    `distances` is the merge distance at each step, closest first. The first
    jump away from the within-voice scale is where different voices would be
    glued together, so those merges are not kept.
    """
    if not distances:
        return 0
    if distances[0] >= _FAR_DISTANCE:
        return 0
    for index in range(1, len(distances)):
        jump = distances[index] - distances[index - 1]
        earlier = distances[:index]
        scale = sorted(earlier)[len(earlier) // 2]
        crossed = jump >= max(0.04, scale * 2.0) and distances[index] >= (scale * 3.0 + 0.02)
        if crossed or distances[index] >= _FAR_DISTANCE:
            return index
    return len(distances)


def _labels_for_many(vectors):
    """Cluster leader centroids without gluing distant voices together.

    Average-linkage on every leader is fine up to a few hundred. Past that,
    cluster a spread of them and attach the rest only when they sit close to
    a centroid. A voice the sample missed stays its own cluster.
    """
    count = len(vectors)
    if count <= _DIRECT_CLUSTER_LIMIT:
        return _agglomerative_labels(vectors)
    step = max(1, count // _DIRECT_CLUSTER_LIMIT)
    chosen = list(range(0, count, step))[:_DIRECT_CLUSTER_LIMIT]
    chosen_vectors = [vectors[index] for index in chosen]
    chosen_labels = _agglomerative_labels(chosen_vectors)
    centroids = {}
    tallies = {}
    for label, index in zip(chosen_labels, chosen):
        vector = _normalize(vectors[index])
        bucket = centroids.setdefault(label, [0.0] * len(vector))
        for dim, value in enumerate(vector):
            bucket[dim] += value
        tallies[label] = tallies.get(label, 0) + 1
    for label, bucket in centroids.items():
        count_label = tallies[label]
        centroids[label] = _normalize([value / count_label for value in bucket])
    # How close a leftover leader must be before it joins. Use the widest
    # within-cluster distance seen in the sample, and never cross _FAR_DISTANCE.
    limit = 0.08
    for label, index in zip(chosen_labels, chosen):
        dist = _cosine_distance(_normalize(vectors[index]), centroids[label])
        if dist * 1.5 > limit:
            limit = min(_FAR_DISTANCE - 0.05, dist * 1.5)
    labels = [None] * count
    chosen_at = {index: label for label, index in zip(chosen_labels, chosen)}
    for index, label in chosen_at.items():
        labels[index] = label
    next_id = (max(chosen_labels) + 1) if chosen_labels else 0
    for index, vector in enumerate(vectors):
        if index in chosen_at:
            continue
        normalized = _normalize(vector)
        best_label = None
        best_distance = None
        for label, centroid in centroids.items():
            dist = _cosine_distance(normalized, centroid)
            if best_distance is None or dist < best_distance:
                best_distance = dist
                best_label = label
        if best_label is not None and best_distance is not None and best_distance <= limit:
            labels[index] = best_label
            continue
        labels[index] = next_id
        centroids[next_id] = normalized
        next_id += 1
    return labels


def _agglomerative_labels(vectors):
    count = len(vectors)
    if count == 0:
        return []
    if count == 1:
        return [0]
    vecs = [_normalize(list(vector)) for vector in vectors]
    width = len(vecs[0])
    for vector in vecs:
        if len(vector) != width:
            raise ValueError("embedding vectors must share a length")
    distances = [[0.0] * count for _ in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            dist = _cosine_distance(vecs[i], vecs[j])
            distances[i][j] = dist
            distances[j][i] = dist
    size = [1] * count
    active = [True] * count
    merges = []
    while sum(active) > 1:
        best = None
        best_distance = None
        for i in range(count):
            if not active[i]:
                continue
            row = distances[i]
            for j in range(i + 1, count):
                if not active[j]:
                    continue
                dist = row[j]
                if best_distance is None or dist < best_distance:
                    best_distance = dist
                    best = (i, j)
        if best is None:
            break
        survivor, absorbed = best
        merges.append((best_distance, survivor, absorbed))
        size_survivor = size[survivor]
        size_absorbed = size[absorbed]
        new_size = size_survivor + size_absorbed
        for other in range(count):
            if other == survivor or other == absorbed or not active[other]:
                continue
            updated = (
                size_survivor * distances[survivor][other] + size_absorbed * distances[absorbed][other]
            ) / new_size
            distances[survivor][other] = updated
            distances[other][survivor] = updated
        size[survivor] = new_size
        active[absorbed] = False
    keep = _merges_to_keep([item[0] for item in merges])
    parent = list(range(count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for _, survivor, absorbed in merges[:keep]:
        parent[find(absorbed)] = find(survivor)
    mapping = {}
    labels = []
    next_id = 0
    for index in range(count):
        root = find(index)
        if root not in mapping:
            mapping[root] = next_id
            next_id += 1
        labels.append(mapping[root])
    return labels


def _leader_compress(windows):
    """Collapse near-duplicate windows so a long file still clusters quickly."""
    vectors = [_normalize(item["vector"]) for item in windows]
    adjacent = []
    for index in range(len(vectors) - 1):
        gap = windows[index + 1]["start"] - windows[index]["end"]
        if gap <= 0.6:
            adjacent.append(_cosine_distance(vectors[index], vectors[index + 1]))
    if not adjacent:
        adjacent = [0.02]
    adjacent.sort()
    typical = adjacent[len(adjacent) // 2]
    threshold = max(typical * 2.5, typical + 0.015, 0.02)
    leaders = []
    for index, vector in enumerate(vectors):
        best_index = None
        best_distance = None
        for leader_index, leader in enumerate(leaders):
            dist = _cosine_distance(vector, leader["centroid"])
            if best_distance is None or dist < best_distance:
                best_distance = dist
                best_index = leader_index
        close = best_index is not None and best_distance is not None and best_distance <= threshold
        if close:
            leader = leaders[best_index]
            count = leader["count"]
            width = len(vector)
            mixed = [
                (leader["centroid"][dim] * count + vector[dim]) / (count + 1) for dim in range(width)
            ]
            leader["centroid"] = _normalize(mixed)
            leader["count"] = count + 1
            leader["indices"].append(index)
            continue
        leaders.append({"centroid": vector, "count": 1, "indices": [index]})
    return [leader["centroid"] for leader in leaders], [leader["indices"] for leader in leaders]


def _labels_to_segments(windows, labels):
    pieces = []
    for window, label in zip(windows, labels):
        pieces.append([window["start"], window["end"], int(label)])
    pieces.sort(key=lambda item: (item[0], item[1], item[2]))
    merged = []
    for start, end, label in pieces:
        if merged and merged[-1][2] == label and start <= merged[-1][1] + 0.35:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end, label])
    totals = {}
    for start, end, label in merged:
        totals[label] = totals.get(label, 0.0) + max(0.0, end - start)
    kept = []
    for start, end, label in merged:
        duration = end - start
        if duration < 0.25 and totals.get(label, 0.0) > duration + 0.2:
            continue
        if duration <= 0:
            continue
        kept.append(_segment(start, end, label))
    return _renumber(kept)


def _builtin_segments(path, max_speakers):
    samples, sample_rate = _read_wav_mono(path)
    windows = _mel_windows(samples, sample_rate)
    if len(windows) < 1:
        return []
    return cluster_windows(windows, max_speakers=max_speakers)


def _read_wav_mono(path):
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2 or channels < 1 or rate < 1:
        raise ValueError("diarization expects 16-bit PCM WAV audio")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    samples = samples / 32768.0
    if rate != 16000 and samples.size:
        duration = samples.size / float(rate)
        target = max(1, int(duration * 16000))
        source_x = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        target_x = np.linspace(0.0, 1.0, num=target, endpoint=False)
        samples = np.interp(target_x, source_x, samples)
        rate = 16000
    return samples, rate


def _mel_filterbank(sample_rate, n_fft, n_mels=24, fmin=70.0, fmax=7600.0):
    import numpy as np

    def hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    high = min(fmax, sample_rate / 2.0 - 100.0)
    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(high), n_mels + 2)
    hz = mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * hz / sample_rate).astype(int)
    bank = np.zeros((n_mels, n_fft // 2 + 1))
    for index in range(n_mels):
        left, center, right = int(bins[index]), int(bins[index + 1]), int(bins[index + 2])
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        for bin_index in range(left, center):
            if 0 <= bin_index < bank.shape[1]:
                bank[index, bin_index] = (bin_index - left) / float(center - left)
        for bin_index in range(center, right):
            if 0 <= bin_index < bank.shape[1]:
                bank[index, bin_index] = (right - bin_index) / float(right - center)
    return bank


def _mel_windows(samples, sample_rate):
    import numpy as np

    if samples is None or len(samples) < sample_rate // 2:
        return []
    n_fft = 512
    hop = 160
    if len(samples) < n_fft:
        return []
    bank = _mel_filterbank(sample_rate, n_fft)
    window = np.hanning(n_fft)
    times = []
    mels = []
    rms = []
    for start in range(0, len(samples) - n_fft + 1, hop):
        frame = samples[start : start + n_fft]
        spectrum = np.abs(np.fft.rfft(frame * window)) ** 2
        mels.append(np.log(bank @ spectrum + 1e-8))
        times.append((start + n_fft / 2.0) / float(sample_rate))
        rms.append(math.sqrt(float(np.mean(frame * frame))))
    times = np.asarray(times)
    mels = np.vstack(mels)
    rms = np.asarray(rms)
    voiced_rms = rms[rms > 0.01]
    if voiced_rms.size:
        threshold = max(0.02, float(np.percentile(voiced_rms, 30)) * 0.5)
    else:
        threshold = 0.02
    voiced = rms >= threshold
    windows = []
    win_s = 0.75
    hop_s = 0.25
    moment = 0.0
    last = float(times[-1]) if len(times) else 0.0
    while moment < last:
        selected = (times >= moment) & (times < moment + win_s)
        count = int(selected.sum())
        if count >= 8:
            flags = voiced[selected]
            if float(flags.mean()) >= 0.85 and int(flags.sum()) >= 6:
                vector = mels[selected][flags].mean(axis=0)
                vector = vector - float(vector.mean())
                norm = float(np.linalg.norm(vector))
                if norm > 1e-6:
                    windows.append(
                        {
                            "start": float(moment),
                            "end": float(min(moment + win_s, last)),
                            "vector": (vector / norm).tolist(),
                        }
                    )
        moment += hop_s
    return windows


_resemblyzer_encoder = None
_speechbrain_classifier = None
_pyannote_pipeline = None
_pyannote_token = ""


def _resemblyzer_segments(path, max_speakers):
    global _resemblyzer_encoder
    from resemblyzer import VoiceEncoder, preprocess_wav

    if _resemblyzer_encoder is None:
        _resemblyzer_encoder = VoiceEncoder("cpu")
    wav = preprocess_wav(path)
    _unused, embeds, slices = _resemblyzer_encoder.embed_utterance(wav, return_partials=True, rate=4)
    del _unused
    windows = []
    for vector, region in zip(embeds, slices):
        start = float(region.start) / 16000.0
        end = float(region.stop) / 16000.0
        windows.append({"start": start, "end": end, "vector": [float(value) for value in vector]})
    return cluster_windows(windows, max_speakers=max_speakers)


def _speechbrain_segments(path, max_speakers):
    global _speechbrain_classifier
    import numpy as np
    import torch

    try:
        from speechbrain.inference.speaker import EncoderClassifier
    except ImportError:
        from speechbrain.pretrained import EncoderClassifier

    if _speechbrain_classifier is None:
        saved = os.path.join(os.path.expanduser("~/.meeting-notes"), "models", "spkrec-ecapa-voxceleb")
        os.makedirs(saved, exist_ok=True)
        _speechbrain_classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=saved,
            run_opts={"device": "cpu"},
        )
    samples, sample_rate = _read_wav_mono(path)
    if sample_rate != 16000:
        raise RuntimeError("speechbrain diarization expects 16 kHz audio")
    win = int(1.25 * sample_rate)
    hop = int(0.6 * sample_rate)
    if len(samples) < win:
        win = len(samples)
        hop = win
    windows = []
    start = 0
    while start < len(samples):
        chunk = samples[start : start + win]
        if len(chunk) < int(0.4 * sample_rate):
            break
        energy = float(np.sqrt(np.mean(chunk * chunk)))
        if energy >= 0.02:
            tensor = torch.tensor(chunk, dtype=torch.float32).unsqueeze(0)
            with torch.no_grad():
                embedding = _speechbrain_classifier.encode_batch(tensor)
            vector = embedding.squeeze().detach().cpu().numpy().astype(float).tolist()
            windows.append(
                {
                    "start": start / float(sample_rate),
                    "end": min(len(samples), start + len(chunk)) / float(sample_rate),
                    "vector": vector,
                }
            )
        if hop <= 0:
            break
        start += hop
    return cluster_windows(windows, max_speakers=max_speakers)


def _pyannote_segments(path, token, max_speakers):
    global _pyannote_pipeline, _pyannote_token
    if not token:
        raise RuntimeError("pyannote needs HF_TOKEN or hf_token in ~/.meeting-notes/config.toml")
    if _pyannote_pipeline is None or _pyannote_token != token:
        from pyannote.audio import Pipeline

        try:
            pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
        except TypeError:
            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1", use_auth_token=token
            )
        try:
            import torch

            pipeline.to(torch.device("cpu"))
        except Exception:
            pass
        _pyannote_pipeline = pipeline
        _pyannote_token = token
    try:
        output = _pyannote_pipeline(path, min_speakers=1, max_speakers=max_speakers)
    except TypeError:
        output = _pyannote_pipeline(path)
    annotation = getattr(output, "speaker_diarization", output)
    segments = []
    for turn, _track, speaker in annotation.itertracks(yield_label=True):
        segments.append(
            {"start": float(turn.start), "end": float(turn.end), "cluster": str(speaker)}
        )
    return segments
