#!/bin/bash
# Install or update Meeting Notes on this Mac. Safe to re-run; no hand edits.
#
#   scripts/install_macos.sh            install / update everything
#   scripts/install_macos.sh --check    only print what would change
#   scripts/install_macos.sh --force    do not wait for an active recording
#
# What it does, in order:
#   1. refuses to touch anything while a call is recording (waits up to 2 h);
#   2. ~/.meeting-notes/venv: installs this repo (non-editable) + sherpa-onnx;
#   3. downloads the two public speaker models (no token) if missing;
#   4. builds the capture helper from native/macos and signs it with a local
#      code-signing identity kept in its own keychain
#      (~/.meeting-notes/signing). The identity never changes, so the
#      designated requirement - and therefore the macOS privacy grants - stay
#      valid across every future rebuild. Installed beside the old helper as
#      MeetingAudioCapture-signed.app; the old helper is never rebuilt;
#   5. migrates config.toml (adds new keys, turns on speaker naming), with a
#      backup; never prints secrets;
#   6. (re)writes the LaunchAgent through `meeting-notes watch --install`
#      (ProcessType Interactive) and restarts the watcher only when idle;
#   7. runs `meeting-notes selftest`.
#
# Once `meeting-notes selftest --verify-signed` passed (needs one Allow click
# for the signed helper), recording uses the signed helper and the old
# helper's tap-fix shim is no longer loaded; this script then removes the
# shim and the old helper on its next run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTROL="${HOME}/.meeting-notes"
VENV="${CONTROL}/venv"
PY="${VENV}/bin/python"
BIN="${CONTROL}/bin"
SIGNED_APP="${BIN}/MeetingAudioCapture-signed.app"
SIGN_DIR="${CONTROL}/signing"
KEYCHAIN="${SIGN_DIR}/meeting-notes-signing.keychain-db"
IDENTITY_NAME="Meeting Notes Local Signing"
CHECK=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Darwin" ]] || { echo "macOS only" >&2; exit 1; }
say_step() { printf '\n== %s\n' "$*"; }

recording() {
  if [[ -f "${CONTROL}/heartbeat.json" ]] && /usr/bin/grep -Eq '"state": *"(recording|joined|in-call)"' "${CONTROL}/heartbeat.json"; then
    return 0
  fi
  /bin/ps -axo command | /usr/bin/grep -E "meeting-audio-capture record|meeting_notes.cli start" | /usr/bin/grep -v grep >/dev/null 2>&1
}

transcribing() {
  /bin/ps -axo command | /usr/bin/grep -E "meeting_notes.cli (process|reprocess|finish)" | /usr/bin/grep -v grep >/dev/null 2>&1
}

wait_idle() {
  [[ $FORCE == 1 ]] && return 0
  local waited=0
  while recording || transcribing; do
    if (( waited == 0 )); then echo "A call is recording or transcribing; waiting (checks every 30 s, up to 2 h)..."; fi
    sleep 30; waited=$((waited + 30))
    if (( waited >= 7200 )); then echo "Still busy after 2 h; not changing anything." >&2; exit 3; fi
  done
}

say_step "1/7 idle check"
if recording; then echo "recording: yes"; else echo "recording: no"; fi
[[ $CHECK == 1 ]] && { echo "--check: stopping before changes"; exit 0; }
wait_idle

say_step "2/7 Python environment (${VENV})"
if [[ ! -x "${PY}" ]]; then
  /usr/bin/python3 -m venv "${VENV}"
fi
"${PY}" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"${PY}" -m pip install -q --force-reinstall --no-deps "${ROOT}"
"${PY}" -m pip install -q "${ROOT}"            # dependencies, if any are missing
"${PY}" -m pip install -q "sherpa-onnx>=1.10" numpy
"${PY}" -c "import meeting_notes, sherpa_onnx; print('package ok, sherpa-onnx', sherpa_onnx.__version__ if hasattr(sherpa_onnx,'__version__') else '')"

say_step "3/7 speaker models"
"${PY}" -m meeting_notes.cli models --download

say_step "4/7 capture helper (stable signing identity)"
mkdir -p "${SIGN_DIR}" "${BIN}"
chmod 700 "${SIGN_DIR}"
if [[ ! -f "${SIGN_DIR}/keychain-password" ]]; then
  /usr/bin/openssl rand -hex 24 > "${SIGN_DIR}/keychain-password"
  chmod 600 "${SIGN_DIR}/keychain-password"
fi
KC_PASS="$(cat "${SIGN_DIR}/keychain-password")"
if [[ ! -f "${KEYCHAIN}" ]]; then
  echo "Creating the local signing identity (once; never leaves this Mac)."
  tmp="$(mktemp -d)"
  cat > "${tmp}/cert.cnf" <<CNF
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=${IDENTITY_NAME}
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
CNF
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -keyout "${tmp}/key.pem" -out "${tmp}/cert.pem" -days 7300 -config "${tmp}/cert.cnf" >/dev/null 2>&1
  /usr/bin/openssl pkcs12 -export -inkey "${tmp}/key.pem" -in "${tmp}/cert.pem" -out "${tmp}/id.p12" -passout "pass:${KC_PASS}" -name "${IDENTITY_NAME}" >/dev/null 2>&1
  security create-keychain -p "${KC_PASS}" "${KEYCHAIN}"
  security set-keychain-settings "${KEYCHAIN}"      # no auto-lock timeout
  security unlock-keychain -p "${KC_PASS}" "${KEYCHAIN}"
  security import "${tmp}/id.p12" -k "${KEYCHAIN}" -P "${KC_PASS}" -T /usr/bin/codesign >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "${KC_PASS}" "${KEYCHAIN}" >/dev/null
  rm -rf "${tmp}"
fi
security unlock-keychain -p "${KC_PASS}" "${KEYCHAIN}"
IDENTITY="$(security find-identity -p codesigning "${KEYCHAIN}" | awk -v n="\"${IDENTITY_NAME}\"" 'index($0, n) {print $2; exit}')"
[[ -n "${IDENTITY}" ]] || { echo "signing identity not found in ${KEYCHAIN}" >&2; exit 1; }
echo "identity: ${IDENTITY_NAME} (${IDENTITY})"

SRC_HASH="$(shasum -a 256 "${ROOT}/native/macos/MeetingAudioCapture.swift" "${ROOT}/native/macos/support/Info.plist" "${ROOT}/native/macos/support/entitlements.plist" | shasum -a 256 | awk '{print $1}')"
STAMP="${SIGNED_APP}/Contents/_source.sha256"
if [[ -f "${STAMP}" && "$(cat "${STAMP}")" == "${SRC_HASH}" ]]; then
  echo "helper source unchanged; keeping the installed signed helper"
else
  SWIFTC="$(command -v swiftc || xcrun --find swiftc)"
  stage="$(mktemp -d)/MeetingAudioCapture-signed.app"
  mkdir -p "${stage}/Contents/MacOS"
  cp "${ROOT}/native/macos/support/Info.plist" "${stage}/Contents/Info.plist"
  "${SWIFTC}" -O \
    -framework AVFoundation -framework CoreAudio -framework AudioToolbox \
    -framework CoreMedia -framework ScreenCaptureKit -framework AppKit -framework CoreGraphics \
    -o "${stage}/Contents/MacOS/meeting-audio-capture" \
    "${ROOT}/native/macos/MeetingAudioCapture.swift"
  codesign --force --options runtime --timestamp=none \
    --entitlements "${ROOT}/native/macos/support/entitlements.plist" \
    --identifier com.meeting-notes.audio-capture \
    --keychain "${KEYCHAIN}" --sign "${IDENTITY}" "${stage}"
  codesign --verify --strict "${stage}"
  printf '%s\n' "${SRC_HASH}" > "${stage}/Contents/_source.sha256"
  rm -rf "${SIGNED_APP}.new"
  mv "${stage}" "${SIGNED_APP}.new"
  rm -rf "${SIGNED_APP}"
  mv "${SIGNED_APP}.new" "${SIGNED_APP}"
  echo "built and signed: ${SIGNED_APP}"
fi
codesign -d -r- "${SIGNED_APP}" 2>/dev/null | /usr/bin/grep designated || true
security lock-keychain "${KEYCHAIN}" || true

say_step "5/7 config"
"${PY}" - <<'PYCFG'
import os, re, shutil, time
path = os.path.expanduser("~/.meeting-notes/config.toml")
if not os.path.exists(path):
    print("no config.toml; copy config.example.toml and fill in webhook_url")
    raise SystemExit(0)
text = open(path, encoding="utf-8").read()
original = text
def has(key):
    return re.search(r"(?m)^\s*%s\s*=" % re.escape(key), text) is not None
# Speaker naming on (the old example said "leave this false").
text = re.sub(r"(?m)^(\s*diarize\s*=\s*)false\s*$", r"\1true", text)
drive = None
cloud = os.path.expanduser("~/Library/CloudStorage")
if os.path.isdir(cloud):
    for name in sorted(os.listdir(cloud)):
        if name.startswith("GoogleDrive-") and os.path.isdir(os.path.join(cloud, name, "My Drive")):
            drive = os.path.join(cloud, name, "My Drive", "Meeting Transcripts")
            break
defaults = {
    "diarize": "true",
    "self_aliases": '""',
    "known_people": '""',
    "organizations": '"Greek, greek.fi, Bebop, Base, Coinbase"',
    "calendar_json_path": '"%s"' % (os.path.join(os.path.dirname(drive), "Meeting Transcripts", "calendar.json") if drive else ""),
    "calendar_ics_url": '""',
    "voice_profiles": "true",
    "transcript_archive_dir": '"%s"' % (drive or ""),
    "delete_audio_requires_delivery": "true",
}
added = []
for key, value in defaults.items():
    if not has(key):
        text += "\n%s = %s" % (key, value)
        added.append(key)
if text != original:
    backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup)
    if not text.endswith("\n"):
        text += "\n"
    open(path, "w", encoding="utf-8").write(text)
    os.chmod(path, 0o600)
    print("config updated (backup %s); added: %s" % (os.path.basename(backup), ", ".join(added) or "-"))
else:
    print("config already current")
if drive:
    os.makedirs(drive, exist_ok=True)
    print("transcript archive folder:", drive)
PYCFG

say_step "6/7 LaunchAgent + watcher"
wait_idle
"${PY}" -m meeting_notes.cli watch --install
if [[ -f "${CONTROL}/bin/.signed-helper-verified.json" ]]; then
  echo "signed helper verified: removing the legacy shim (kept as a backup in ${CONTROL}/backups)"
  mkdir -p "${CONTROL}/backups"
  if [[ -d "${CONTROL}/shim" ]]; then mv "${CONTROL}/shim" "${CONTROL}/backups/shim-$(date +%Y%m%d%H%M%S)"; fi
fi

say_step "7/7 selftest"
"${PY}" -m meeting_notes.cli selftest || true
if [[ ! -f "${CONTROL}/bin/.signed-helper-verified.json" ]]; then
  cat <<MSG

One-time step to switch to the signed helper (permissions then survive all
future updates):  ${PY} -m meeting_notes.cli selftest --verify-signed
and click "Allow" when macOS asks whether "Meeting Audio Capture" may record
audio (system audio and microphone). Until then the current helper keeps
recording with the tap-fix shim, so nothing breaks.
MSG
fi
