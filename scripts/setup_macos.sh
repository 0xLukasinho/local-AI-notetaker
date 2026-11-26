#!/bin/bash
# Kept for old instructions. The installer/updater is scripts/install_macos.sh.
# (This script used to rebuild the ad-hoc signed helper in place, which
# silently invalidated its macOS privacy grants on every source change.)
exec "$(cd "$(dirname "$0")" && pwd)/install_macos.sh" "$@"
