#!/usr/bin/env bash
# devproc uninstaller, per project directory. Dry run unless --apply.
# Removes only what devproc installed and the project did not change.
#   ./uninstall.sh [--apply] <target-dir>
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -I "$here/lib/devproc_install.py" uninstall "$@"
