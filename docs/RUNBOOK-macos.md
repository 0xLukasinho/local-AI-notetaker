# Meeting Notes on macOS: runbook

## Install or update (the only supported way)

```bash
scripts/install_macos.sh          # install / update; waits while a call records
scripts/install_macos.sh --check  # show state, change nothing
```

The installer is idempotent. It never rebuilds the old helper in place, never
edits the LaunchAgent by hand, and never prints secrets. Steps: idle check ->
venv + package (non-editable) + sherpa-onnx -> speaker models -> capture helper
built from `native/macos` and signed with a stable local identity -> config
migration (with a backup) -> LaunchAgent via `meeting-notes watch --install`
(ProcessType Interactive) -> `meeting-notes selftest`.

## Health check

```bash
~/.meeting-notes/venv/bin/meeting-notes selftest            # static checks
~/.meeting-notes/venv/bin/meeting-notes selftest --capture  # + real capture of a 3-voice clip
~/.meeting-notes/venv/bin/meeting-notes selftest --e2e      # + transcribe and name the speakers
```

PASS / WARN / FAIL lines with evidence: LaunchAgent ProcessType, watcher
heartbeat and scheduling priority (Background throttling = pri 4, lost ~50 %
of audio on Nov 24), which helper records and its designated requirement,
speaker models, webhook/archive/calendar settings (values never shown), and
the last five sessions. The capture checks refuse to run while a call records.

Logs: `~/.meeting-notes/watch.log` (watcher), `<session>/recorder.log`,
`<session>/resume.log`, `~/.meeting-notes/launchd.err.log`.

## Capture: why the remote track was silent, and the permanent fix

* The helper set `CATapDescription.isExclusive = false` after
  `stereoGlobalTapButExcludeProcesses: []`. Exclusive means "everything except
  the list", so false turned the empty exclusion list into an empty inclusion
  list: the tap captured no process and wrote digital silence. Fixed in
  `native/macos/MeetingAudioCapture.swift` (plus App Nap opt-out, a capped mix
  buffer, and a writer paced by the microphone clock).
* The installed helper from Nov 16 is ad-hoc signed. macOS privacy grants for
  an ad-hoc binary are keyed to its exact hash, so any rebuild silently drops
  them. Until the signed helper is verified, the old helper keeps recording
  with `~/.meeting-notes/shim/libtapfix.dylib` (forces the tap exclusive at
  run time; the binary and its grants stay unchanged).

### TCC-safe install path (signed helper)

`install_macos.sh` creates a self-signed code-signing certificate in its own
keychain (`~/.meeting-notes/signing/`, random password in a 0600 file, no
login-keychain changes, no prompts) and signs
`~/.meeting-notes/bin/MeetingAudioCapture-signed.app` with it (hardened
runtime, microphone entitlement, identifier `com.meeting-notes.audio-capture`).
Its designated requirement is
`identifier "com.meeting-notes.audio-capture" and certificate leaf = H"<cert>"`,
which does not change when the helper is rebuilt, so grants survive updates.

One-time switch (the only manual step, ever):

```bash
~/.meeting-notes/venv/bin/meeting-notes selftest --verify-signed
```

Click **Allow** when macOS asks whether *Meeting Audio Capture* may record
audio. The command records a test clip with the signed helper; only if the
remote track is non-silent and >= 95 % of wall clock does it write
`~/.meeting-notes/bin/.signed-helper-verified.json`. From then on recording
uses the signed helper (no shim is loaded), and the next installer run moves
the shim to `~/.meeting-notes/backups/`. Window-title probing keeps using the
old helper unless the signed one also has Screen Recording.

### ScreenCaptureKit fallback (TCC -3801)

Recording uses the Core Audio process tap. If the tap delivers only digital
silence for 15 s (often just the seconds before anyone in the call speaks),
the helper tries ScreenCaptureKit. Without a *Screen & System Audio
Recording* grant for `MeetingAudioCapture-signed` that fails with
`SCStreamErrorDomain -3801` and the helper keeps the tap, which picks up the
call as soon as Chrome plays audio. The recording is not affected. To make
the fallback usable, grant the signed helper once in System Settings >
Privacy & Security > Screen & System Audio Recording; `selftest` shows a WARN
until then.

### Audio is kept until names are confident

WAVs are deleted only after the transcript is verified, delivered/archived,
and every speaker in `speaker_map` is `confirmed`. Otherwise status.json gets
`needs_relabel: true`, `unconfirmed_speakers`, and `audio_kept_reason`, and
`meeting-notes reprocess FOLDER --participants "A, B"` can re-label it.

## Speaker names

Mic track = you (`self_name`). The remote track is split into voices with
sherpa-onnx (pyannote segmentation 3.0 + WeSpeaker ResNet34 embeddings, ONNX,
CPU, no token): each Whisper utterance gets an embedding and utterances are
grouped by average-linkage cosine similarity. Each voice is then named from:

| Source | Example | Weight |
|---|---|---|
| Calendar attendees (`calendar_json_path` / `calendar_ics_url`) | one remote attendee + one voice | 0.9 |
| Meet window title | `Partnerships Sync (Alex Rivera)` | roster |
| Slack DM / group DM title | `Mahmoud (DM) - Greek - Slack` | roster |
| `roster.json` in the session folder | `["Alex Rivera"]` | roster |
| Self-introduction | "Hi, this is Alex from Northwind" | 0.95 listed / 0.8 not listed |
| Addressed by name | "Thanks, Priya" before Priya answers | 0.4 each |
| Voice profile from earlier calls | cosine >= 0.70 | 0.75-0.95 |
| Elimination | last voice, last attendee | 0.75 |

A voice is named at confidence >= 0.7. Otherwise it is `Speaker N` or
`Speaker N (unconfirmed: likely X)`. A name nobody said, listed or confirmed
is never produced; nobody gets a name they addressed; your own names
(`self_name`, `self_aliases`) never go to a remote voice. Whisper spelling
drift ("Mahmood") is mapped to the listed spelling, and people's names are
added to the Whisper glossary.

`transcript.json` has `speaker_map` (`display`, `name`, `confidence`,
`status`, `evidence`) and `speaker_naming` (roster and its sources);
`status.json` has a short `speaker_map`; every transcript line carries the name.

Fix or confirm a voice after the fact (also saves the voice profile):

```bash
meeting-notes voices show 2026-11-25_executive
meeting-notes voices name 2026-11-25_executive "Speaker 3" "Mahmoud"
meeting-notes voices list | forget NAME
```

Profiles (`~/.meeting-notes/voices/profiles.json`, 0600) hold 256-float
embeddings only, never audio, and are updated only from confirmed voices
(confidence >= 0.85 or named by you).

## Delivery and audio retention

* Webhook: JSON bodies <= `webhook_max_body_bytes` with `chunk_index`,
  `chunk_count`, `transcript_sha256`; a receiver that echoes a different
  checksum fails the delivery (retried, kept in the outbox).
* `transcript_archive_dir` (the installer points it at
  `My Drive/Meeting Transcripts`): every call's complete `transcript.txt`,
  `transcript.json` and `transcript.sha256`, verified after writing. Its path
  is sent as `transcript_archive_path`.
* WAVs are deleted only when the transcript is verified (non-empty, has
  segments, not mic-only) **and**, for watcher calls, delivered or archived.
  Otherwise `status.json` says `audio_kept` and why, and
  `meeting-notes reprocess` can redo it.
* A recording that transcribes to nothing is not re-transcribed on every
  watcher start any more.

## Tests

```bash
python -m pytest -q                      # unit + integration (never touches the live LaunchAgent)
MEETING_NOTES_E2E=1 python -m pytest tests/test_e2e_capture.py -s   # real capture on this Mac
```

`tests/conftest.py` points `HOME` at a temp dir and fails any test that runs
`launchctl`.
