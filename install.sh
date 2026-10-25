#!/usr/bin/env bash
# devproc installer, per project directory. Dry run unless --apply.
#   ./install.sh [--apply] <target-dir>            install
#   ./install.sh --upgrade [--apply] <target-dir>  upgrade (keeps files the project changed)
#   ./install.sh --status <target-dir>             show state, never writes
#   ./install.sh --repair [--apply] <target-dir>   restore a damaged, moved or copied install (same version)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd=install
args=()
for a in "$@"; do
  case "$a" in
    --upgrade) cmd=upgrade ;;
    --status) cmd=status ;;
    --repair) cmd=repair ;;
    *) args+=("$a") ;;
  esac
done
exec python3 -I "$here/lib/devproc_install.py" "$cmd" ${args[@]+"${args[@]}"}
