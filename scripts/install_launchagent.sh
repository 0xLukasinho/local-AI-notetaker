#!/bin/bash
# Install the per-user LaunchAgent (login, wake, unlock).
# scripts/setup_macos.sh calls this by running `meeting-notes autostart install`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "scripts/install_launchagent.sh is for macOS." >&2
  exit 1
fi
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi
exec "${PY}" -m meeting_notes.cli autostart install
