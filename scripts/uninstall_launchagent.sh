#!/bin/bash
# Remove the per-user LaunchAgent.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "scripts/uninstall_launchagent.sh is for macOS." >&2
  exit 1
fi
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi
exec "${PY}" -m meeting_notes.cli autostart uninstall
