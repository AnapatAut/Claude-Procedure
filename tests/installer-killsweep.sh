#!/usr/bin/env bash
# Kill sweep: a run of install, upgrade or uninstall is SIGKILLed before each of its steps (and
# after the last), as kill -9, the OOM killer or a power cut would do it (DEVPROC_FAILPOINT=step:<k>).
# After each kill:
#   - the project's own data is intact: settings.local.json parses and keeps its keys, and the
#     git exclude file keeps the project's patterns;
#   - rerunning the same command, and running the commands it prints, gets to a done state
#     (install and upgrade: --status healthy; uninstall: done);
#   - a final uninstall leaves the directory as it was before the first install, byte for byte, and
#     no record of it in the registry. Allowed difference, as docs/03 says: rule files a killed
#     install had already copied count as the project's own and stay (with their empty parents).
# Same sandbox as installer-e2e.sh: a throwaway CLAUDE_CONFIG_DIR and hermetic git.
#   tests/installer-killsweep.sh            exit 0 = every check passed
#   E2E_FAIL_FAST=1 tests/installer-killsweep.sh   stop at the first failed check
set -euo pipefail
REAL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d -t devproc-sweep-XXXXXX)"
trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT
cp -r "$REAL_SRC" "$T/src"
SRC="$T/src"
if git -C "$T" rev-parse --git-dir >/dev/null 2>&1; then
  echo "installer-killsweep: the temp dir $T is inside a git work tree; set TMPDIR to a folder outside any repo"; exit 2
fi
export CLAUDE_CONFIG_DIR="$T/cfg"
mkdir -p "$CLAUDE_CONFIG_DIR"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN DEVPROC_FAILPOINT
fails=0
ok()   { echo "ok   $*"; }
bad()  { echo "BAD  $*"; fails=$((fails+1)); if [ -n "${E2E_FAIL_FAST:-}" ]; then exit 1; fi; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
snap() { (cd "$1" && find . -path ./.git/objects -prune -o -printf '%y %m %p\n' | sort \
  && find . -path ./.git/objects -prune -o -type f -print0 | sort -z | xargs -0 -r sha256sum); cat "$1/.git/info/exclude" 2>/dev/null || true; }
setver() { python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["version"]=sys.argv[2]; open(p,"w").write(json.dumps(d))' "$SRC/plugin/.claude-plugin/plugin.json" "$1"; }
BASE="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$SRC/plugin/.claude-plugin/plugin.json")"
# a project with its own settings (CRLF) and exclude patterns (no final newline)
fresh() { rm -rf "$1"; mkdir -p "$1/.claude"; git -C "$1" init -q
  printf '{\r\n  "env": {"MY_VAR": "1"}\r\n}\r\n' > "$1/.claude/settings.local.json"
  printf '*.log\nbuild/' > "$1/.git/info/exclude"; echo "project" > "$1/README.md"; }
data_ok() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("env") == {"MY_VAR": "1"} else 1)' "$1/.claude/settings.local.json" \
  && grep -qx '\*\.log' "$1/.git/info/exclude" && grep -qx 'build/' "$1/.git/info/exclude"; }
total() { "$SRC/$1" "${@:2}" 2>/dev/null | grep -oE '\[[0-9]+/[0-9]+\]' | head -1 | sed 's#.*/##; s#]##'; }
# rerun "$@" until it exits 0, running the fix commands each refusal prints ("      what: command")
recover() { local log="$T/recover.log" n; for n in 1 2 3 4; do
    set +e; "$@" > "$log" 2>&1; local rc=$?; set -e
    [ "$rc" = 0 ] && return 0
    local cmds; cmds="$(sed -n 's/^      [^:]*: \(.*\)$/\1/p' "$log")"
    [ -n "$cmds" ] || { cat "$log"; return 1; }
    bash -c "$cmds" > /dev/null 2>&1 || true
  done; cat "$log"; return 1; }
registry_clean() { python3 -I - "$CLAUDE_CONFIG_DIR" "$1" <<'PY'
import json, os, sys
cfg, d = sys.argv[1], os.path.realpath(sys.argv[2])
km = json.load(open(os.path.join(cfg, "plugins", "known_marketplaces.json"))) if os.path.exists(os.path.join(cfg, "plugins", "known_marketplaces.json")) else {}
ip = json.load(open(os.path.join(cfg, "plugins", "installed_plugins.json"))) if os.path.exists(os.path.join(cfg, "plugins", "installed_plugins.json")) else {}
bad = [k for k, v in km.items() if d in json.dumps(v)]
bad += [k for k, v in ip.get("plugins", {}).items() for r in v if os.path.realpath(r.get("projectPath", "")) == d]
sys.exit(1 if bad else 0)
PY
}
# the comparison: drop rule files identical to a template (copied by a killed install, kept as the
# project's own, as docs/03 says) and folders left empty by that
as_before() { local d="$1" f
  for f in "$d"/.claude/rules/devproc/*.md; do
    [ -f "$f" ] && cmp -s "$f" "$SRC/plugin/templates/rules/$(basename "$f")" && rm -f "$f"
  done
  rmdir "$d/.claude/rules/devproc" "$d/.claude/rules" 2>/dev/null || true
  [ "$(snap "$d")" = "$2" ]; }

sweep() { local cmd="$1" k n d
  echo "== kill sweep: $cmd"
  d="$T/probe-$cmd"; fresh "$d"
  case "$cmd" in
    install) n="$(total install.sh "$d")" ;;
    upgrade) "$SRC/install.sh" --apply "$d" > /dev/null; setver 9.9.90; n="$(total install.sh --upgrade "$d")"; setver "$BASE"
             "$SRC/uninstall.sh" --apply "$d" > /dev/null ;;
    uninstall) "$SRC/install.sh" --apply "$d" > /dev/null; n="$(total uninstall.sh "$d")"; "$SRC/uninstall.sh" --apply "$d" > /dev/null ;;
  esac
  [ -n "$n" ] || { bad "$cmd: could not count its steps"; return; }
  for k in $(seq 1 $((n + 1))); do
    d="$T/$cmd-$k"; fresh "$d"; local before; before="$(snap "$d")"
    case "$cmd" in
      install) set +e; DEVPROC_FAILPOINT="step:$k" "$SRC/install.sh" --apply "$d" > "$T/kill.log" 2>&1; local rc=$?; set -e ;;
      upgrade) setver "$BASE"; "$SRC/install.sh" --apply "$d" > /dev/null || bad "$cmd $k: setup install"
               setver 9.9.90
               set +e; DEVPROC_FAILPOINT="step:$k" "$SRC/install.sh" --upgrade --apply "$d" > "$T/kill.log" 2>&1; local rc=$?; set -e ;;
      uninstall) "$SRC/install.sh" --apply "$d" > /dev/null || bad "$cmd $k: setup install"
                 set +e; DEVPROC_FAILPOINT="step:$k" "$SRC/uninstall.sh" --apply "$d" > "$T/kill.log" 2>&1; local rc=$?; set -e ;;
    esac
    check "$cmd killed before step $k/$n: killed (exit 137)" test "$rc" = 137
    check "$cmd killed before step $k/$n: the project's settings and exclude patterns intact" data_ok "$d"
    case "$cmd" in
      install) { recover "$SRC/install.sh" --apply "$d" && [ "$("$SRC/install.sh" --status "$d" >/dev/null 2>&1; echo $?)" = 0 ] \
                 && ok "$cmd killed before step $k/$n: recovered, healthy" \
                 || bad "$cmd killed before step $k/$n: recovery to a healthy install"; } ;;
      upgrade) { recover "$SRC/install.sh" --upgrade --apply "$d" && [ "$("$SRC/install.sh" --status "$d" >/dev/null 2>&1; echo $?)" = 0 ] \
                 && grep -q '"version": "9.9.90"' "$d/.claude/devproc/manifest.json" && ok "$cmd killed before step $k/$n: recovered, healthy at the new version" \
                 || bad "$cmd killed before step $k/$n: recovery to a healthy upgrade"; } ;;
      uninstall) { recover "$SRC/uninstall.sh" --apply "$d" && ok "$cmd killed before step $k/$n: recovered (uninstall done)" \
                   || bad "$cmd killed before step $k/$n: recovery of the uninstall"; } ;;
    esac
    [ "$cmd" = uninstall ] || "$SRC/uninstall.sh" --apply "$d" > "$T/final.log" 2>&1 || { cat "$T/final.log"; bad "$cmd $k: final uninstall"; }
    check "$cmd killed before step $k/$n: after uninstall, the directory as before (byte for byte)" as_before "$d" "$before"
    check "$cmd killed before step $k/$n: no registry record of the directory left" registry_clean "$d"
  done
  setver "$BASE"
}

sweep install
sweep upgrade
sweep uninstall
echo "installer-killsweep: $fails failure(s)"
[ "$fails" -eq 0 ]
