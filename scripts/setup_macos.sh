#!/bin/bash
# Set up meeting-notes on a Mac that has the Xcode command-line tools.
# Homebrew is not required. The script creates a venv, installs this repo
# into it, builds the audio helper, copies the example config if you do not
# already have one, and installs the LaunchAgent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "scripts/setup_macos.sh is for macOS." >&2
  exit 1
fi

echo "Meeting Notes macOS setup"
echo "Repo: $ROOT"

python_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && python_ok "$(command -v "$candidate")"; then
    PY="$(command -v "$candidate")"
    break
  fi
done

if [[ -z "$PY" ]]; then
  echo "Python 3.9+ was not found. Installing uv (no Homebrew) to provide Python 3.12."
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is missing, so Python cannot be downloaded. Install Python 3.9+ from https://www.python.org/downloads/macos/ and run this script again." >&2
    exit 1
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv did not install. Install Python 3.9+ from https://www.python.org/downloads/macos/ and run this script again." >&2
    exit 1
  fi
  uv python install 3.12
  PY="$(uv python find 3.12)"
fi

echo "Using Python: $PY"

"$PY" -m venv "${ROOT}/.venv"
VENV_PY="${ROOT}/.venv/bin/python"
"${VENV_PY}" -m pip install --upgrade pip
"${VENV_PY}" -m pip install -e "${ROOT}"

BIN_DIR="${HOME}/.meeting-notes/bin"
APP_DIR="${BIN_DIR}/MeetingAudioCapture.app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"
mkdir -p "${MACOS_DIR}" "${HOME}/.meeting-notes"

cat > "${APP_DIR}/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>Meeting Audio Capture</string>
  <key>CFBundleDisplayName</key>
  <string>Meeting Audio Capture</string>
  <key>CFBundleIdentifier</key>
  <string>com.meeting-notes.audio-capture</string>
  <key>CFBundleExecutable</key>
  <string>meeting-audio-capture</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Meeting Notes records your microphone so the transcript includes what you say. Other people in the call do not see this.</string>
  <key>NSAudioCaptureUsageDescription</key>
  <string>Meeting Notes records system audio on this Mac so the transcript includes other people. Nothing joins the meeting.</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>Meeting Notes reads window titles to tell when a Google Meet tab is active, and can capture system audio. The screen is not saved.</string>
</dict>
</plist>
EOF

echo "Building the audio helper with swiftc (Xcode command-line tools)..."
if ! command -v swiftc >/dev/null 2>&1 && ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "swiftc was not found. Install the Xcode command-line tools with: xcode-select --install" >&2
  exit 1
fi
SWIFTC="$(command -v swiftc || true)"
if [[ -z "${SWIFTC}" ]]; then
  SWIFTC="$(xcrun --find swiftc)"
fi
"${SWIFTC}" -O \
  -framework AVFoundation \
  -framework CoreAudio \
  -framework AudioToolbox \
  -framework CoreMedia \
  -framework ScreenCaptureKit \
  -framework AppKit \
  -framework CoreGraphics \
  -o "${MACOS_DIR}/meeting-audio-capture" \
  "${ROOT}/native/macos/MeetingAudioCapture.swift"
codesign --force --sign - "${APP_DIR}"
chmod +x "${MACOS_DIR}/meeting-audio-capture"
ln -sfn "${MACOS_DIR}/meeting-audio-capture" "${BIN_DIR}/meeting-audio-capture"
echo "Helper: ${MACOS_DIR}/meeting-audio-capture"

if ! command -v ffmpeg >/dev/null 2>&1 && [[ ! -x "${BIN_DIR}/ffmpeg" ]]; then
  echo "ffmpeg was not found. It is only the fallback recorder. Trying the evermeet.cx static build (no Homebrew)..."
  if command -v curl >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    if curl -fL "https://evermeet.cx/ffmpeg/getrelease/zip" -o "${tmp}/ffmpeg.zip"; then
      unzip -qo "${tmp}/ffmpeg.zip" -d "${BIN_DIR}" || true
      chmod +x "${BIN_DIR}/ffmpeg" 2>/dev/null || true
    fi
    rm -rf "${tmp}"
  fi
  if [[ ! -x "${BIN_DIR}/ffmpeg" ]] && ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg is still missing. The system-audio helper does not need it."
    echo "If the helper cannot start, install ffmpeg with: brew install ffmpeg"
  fi
fi

if [[ ! -f "${HOME}/.meeting-notes/config.toml" && ! -f "${HOME}/.meeting-notes/config.json" ]]; then
  cp "${ROOT}/config.example.toml" "${HOME}/.meeting-notes/config.toml"
  echo "Copied config.example.toml to ${HOME}/.meeting-notes/config.toml"
  echo "Edit webhook_url and webhook_auth_value in that file. Do not commit it."
else
  echo "Left the existing config in ${HOME}/.meeting-notes alone."
fi

ZSHRC="${HOME}/.zshrc"
PATH_LINE="export PATH=\"${ROOT}/.venv/bin:${BIN_DIR}:\$PATH\""
if [[ -f "${ZSHRC}" ]] && grep -F "${ROOT}/.venv/bin" "${ZSHRC}" >/dev/null 2>&1; then
  echo "PATH already includes the venv."
else
  printf '\n# meeting-notes\n%s\n' "${PATH_LINE}" >> "${ZSHRC}"
  echo "Added the venv and helper bin to ${ZSHRC}"
fi

echo "Installing the LaunchAgent..."
"${VENV_PY}" -m meeting_notes.cli autostart install

echo
echo "One-time Chrome extension (same extension as Windows):"
"${VENV_PY}" -m meeting_notes.cli speakers

cat <<EOF

Permission dialogs macOS will show
----------------------------------
The first recording, and the first time the watcher looks for a Meet, macOS
asks you to allow Meeting Audio Capture (or Terminal, if you started a
recording from Terminal before the helper was signed):

  1. Microphone
     "Meeting Audio Capture would like to access the microphone."
     Click Allow.

  2. Screen & System Audio Recording
     System Settings → Privacy & Security → Screen & System Audio Recording.
     Turn on Meeting Audio Capture.
     This is what lets the watcher read the Meet window title and record
     call audio. The screen is not saved. Other people in the Meet do not
     see a bot.

If a dialog is easy to miss, open System Settings and enable both toggles,
then run:

  ${VENV_PY} -m meeting_notes.cli watch --ensure

Open a new terminal so PATH includes:

  ${ROOT}/.venv/bin

Then:

  meeting-notes watch --status
  meeting-notes autostart status

BlackHole is not installed. AVFoundation cannot hear other applications.
The helper uses a Core Audio process tap, then ScreenCaptureKit. Set
MEETING_NOTES_CAPTURE=avfoundation only if you already have a virtual device
and point MEETING_NOTES_AVFOUNDATION_DEVICE at it.

Transcription is the existing faster-whisper path. On this Mac there is no
CUDA device, so the medium model runs on CPU (int8). Summaries still use
the Claude Code CLI.
EOF
