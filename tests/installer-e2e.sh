#!/usr/bin/env bash
# End-to-end test of install.sh / uninstall.sh in a sandbox.
# Uses a throwaway CLAUDE_CONFIG_DIR and throwaway target dirs under a temp dir:
# the real ~/.claude and real projects are never touched.
#   tests/installer-e2e.sh            exit 0 = every check passed
# The 0.2.0-migration checks need the devproc 0.2.0 source: set OLD_SRC to it, or run
# from a git clone that has the v0.2.0 tag. Without it they are reported as SKIPPED
# and the run fails.
#   E2E_FAIL_FAST=1 tests/installer-e2e.sh   stop at the first failed check (used by installer-mutants.py)
set -euo pipefail
REAL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d -t devproc-e2e-XXXXXX)"
trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT
# run against a throwaway copy, so the upgrade test can change the version
cp -r "$REAL_SRC" "$T/src"
SRC="$T/src"
if git -C "$T" rev-parse --git-dir >/dev/null 2>&1; then
  echo "installer-e2e: the temp dir $T is inside a git work tree; set TMPDIR to a folder outside any repo"; exit 2
fi
export CLAUDE_CONFIG_DIR="$T/cfg"
mkdir -p "$CLAUDE_CONFIG_DIR"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
# no global ignore file either (git reads ~/.config/git/ignore even without a global config)
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.excludesFile GIT_CONFIG_VALUE_0=/dev/null
# no login in the sandbox: test sessions stop after loading plugins and never reach the API
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
fails=0
ok()   { echo "ok   $*"; }
bad()  { echo "BAD  $*"; fails=$((fails+1)); if [ -n "${E2E_FAIL_FAST:-}" ]; then exit 1; fi; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
# every path under $1 with its type and mode, every file's hash, and the git exclude file
snap() { (cd "$1" && find . -path ./.git/objects -prune -o -printf '%y %m %p\n' | sort \
  && find . -path ./.git/objects -prune -o -type f -print0 | sort -z | xargs -0 -r sha256sum); cat "$1/.git/info/exclude" 2>/dev/null || true; }
inst() { "$SRC/install.sh" "$@"; }
uninst() { "$SRC/uninstall.sh" "$@"; }
mf() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]+"/.claude/devproc/manifest.json"))[sys.argv[2]])' "$1" "$2"; }
setver() { python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["version"]=sys.argv[2]; open(p,"w").write(json.dumps(d))' "$SRC/plugin/.claude-plugin/plugin.json" "$1"; }
# folder Claude Code loads directory $1's devproc plugin from (claude plugin list --json readFromFolder)
loaded() { local id; id="$(mf "$1" pluginId)"; (cd "$1" && claude plugin list --json) | python3 -I -c '
import json, os, sys
d = os.path.realpath(sys.argv[1])
for p in json.load(sys.stdin):
    if p["id"] == sys.argv[2] and os.path.realpath(p.get("projectPath", "")) == d:
        print(p.get("readFromFolder", "")); break' "$1" "$id"; }
# hooks.json a real session started in $1 reads. The sandbox has no login, so the session stops
# after Claude Code has loaded its plugins; the debug log records what it loaded.
session_hooks() { rm -rf "$CLAUDE_CONFIG_DIR/debug"; (cd "$1" && timeout 120 claude -p hi --debug </dev/null >/dev/null 2>&1) || true
  grep -h "Read hooks.json for plugin devproc" "$CLAUDE_CONFIG_DIR"/debug/*.txt 2>/dev/null | sed 's/.*: //' | tail -1; }
registered() { claude plugin marketplace list --json | python3 -I -c 'import json,sys; [print(m["name"], m.get("path") or "") for m in json.load(sys.stdin)]'; }
reg_has() { registered | grep -q "^$1 "; }
reg_lacks() { local r; r="$(registered)" || return 1; ! printf '%s\n' "$r" | grep -q "^$1 "; }
mname() { printf 'devproc-%s' "$(printf %s "$(realpath "$1")" | sha256sum | cut -c1-8)"; }
# devproc install records in the sandbox registry: "<id> <scope> <projectPath>" per line; fails if unreadable
recs() { python3 -I -c '
import json, sys
import os
for k, v in json.load(open(sys.argv[1])).get("plugins", {}).items():
    if k.startswith("devproc@"):
        for r in v: print(k, r.get("scope"), os.path.realpath(r["projectPath"]) if r.get("projectPath") else "")' "$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json"; }
has_rec() { local r; r="$(recs)" || return 1; printf '%s\n' "$r" | grep -qxF "$1 local $(realpath -m "$2")"; }
lacks_rec() { local r; r="$(recs)" || return 1; ! printf '%s\n' "$r" | grep -qxF "$1 local $(realpath -m "$2")"; }
no_recs_at() { local r; r="$(recs)" || return 1; ! printf '%s\n' "$r" | grep -qF " local $(realpath -m "$1")" ; }
no_recs() { local r; r="$(recs)" || return 1; test -z "$r"; }
# a directory plus Claude Code's registry: what a dry run or a refusal must leave as it was
state() { snap "$1"; registered || echo "registry unreadable $RANDOM$RANDOM"; recs 2>/dev/null || echo "no install records file"; }
list_lacks_dir() { local r; r="$(cd "$1" && claude plugin list --json)" || return 1; ! printf '%s' "$r" | grep -qF "\"$1\""; }
# install.sh --apply $1 must exit 1 and print $2
refused() { local rc; set +e; "$SRC/install.sh" --apply "$1" > "$T/refuse.log" 2>&1; rc=$?; set -e; [ "$rc" = 1 ] && grep -qF -- "$2" "$T/refuse.log"; }
# fake claude in $T/fakebin: fail the call whose arguments start with the given words.
# "once": fail only the first such call. "after": run the real command first, then report failure
# (a step that did part of its work).
fake_fail() { local once="" after=""; while :; do case "$1" in once) once=1; shift ;; after) after=1; shift ;; *) break ;; esac; done
rm -f "$T/failed-once"; cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
case "\$*" in "$*"*) if [ -z "$once" ] || [ ! -e "$T/failed-once" ]; then touch "$T/failed-once"; if [ -n "$after" ]; then "$real" "\$@" >/dev/null 2>&1; fi; echo "simulated failure" >&2; exit 1; fi ;; esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"; }
# "k/N" of the last numbered step a run printed: a full run prints k = N
steps_done() { grep -oE '\[[0-9]+/[0-9]+\]' "$1" | tail -1 | tr -d '[]'; }
# dry run log $1 lists exactly the steps the applied run $2 did, all of them
same_steps() { local d a; d="$(steps_done "$1")"; a="$(steps_done "$2")"; [ -n "$d" ] && [ "$d" = "$a" ] && [ "${d%/*}" = "${d#*/}" ]; }
# file $1 exists and does not contain $2
lacks() { [ -f "$1" ] && ! grep -q -- "$2" "$1"; }
writable_in() { find "$1" -perm -u=w -print -quit; }
status_rc() { set +e; inst --status "$1" > "$T/status.log" 2>&1; local rc=$?; set -e; echo "$rc"; }

# ---- targets
P1="$T/p1"; mkdir -p "$P1"; git -C "$P1" init -q
P2="$T/p2"; mkdir -p "$P2"                       # not a git repo
P3="$T/p3"; mkdir -p "$P3/.claude/rules/devproc"; git -C "$P3" init -q
echo "# my own rules" > "$P3/.claude/rules/devproc/10-modes.md"
cp "$SRC/plugin/templates/rules/00-precedence.md" "$P3/.claude/rules/devproc/00-precedence.md"
echo '{"permissions":{"allow":["Bash(ls *)"]}}' > "$P3/.claude/settings.local.json"

echo "== p1: dry run writes nothing"
mkdir -p "$T/fakebin"; real="$(command -v claude)"
before="$(state "$P1")"
inst "$P1" > "$T/dry.log"
check "dry run: output says dry run" grep -q "dry run only" "$T/dry.log"
check "dry run: nothing changed, registry included" test "$before" = "$(state "$P1")"

echo "== p1: install"
inst --apply "$P1" > "$T/i1.log" || { cat "$T/i1.log"; bad "install exit"; }
check "rules seeded" test -f "$P1/.claude/rules/devproc/00-precedence.md"
check "config seeded" test -f "$P1/.claude/devproc/config.json"
check "MISTAKES seeded" test -f "$P1/.claude/devproc/MISTAKES.md"
check "manifest written" test -f "$P1/.claude/devproc/manifest.json"
check "plugin enabled locally, under this directory's own marketplace" grep -q "\"devproc@$(mname "$P1")\": true" "$P1/.claude/settings.local.json"
check "git exclude has devproc block" grep -q "added by devproc" "$P1/.git/info/exclude"
check "git status clean (all ignored)" test -z "$(git -C "$P1" status --porcelain)"
check "dry run listed every step the install did" same_steps "$T/dry.log" "$T/i1.log"
check "post-check ran" grep -q "post-check: devproc@devproc-[0-9a-f]* .* enabled for this directory, loaded from its own snapshot" "$T/i1.log"
P1ID="$(mf "$P1" pluginId)"

echo "== p1: second install changes nothing"
before="$(state "$P1")"
inst --apply "$P1" > "$T/i2.log"
check "second install: already installed" grep -q "already installed" "$T/i2.log"
check "second install: nothing changed" test "$before" = "$(state "$P1")"

echo "== p1: project evolves a rule; status + upgrade keep it"
echo "- project rule: always run e2e first" >> "$P1/.claude/rules/devproc/30-work.md"
inst --status "$P1" > "$T/st.log"
check "status: evolved file reported" grep -q "30-work.md.*evolved by project" "$T/st.log"
inst --upgrade --apply "$P1" > "$T/up.log" || { cat "$T/up.log"; bad "upgrade exit"; }
check "upgrade: evolved rule kept" grep -q "always run e2e first" "$P1/.claude/rules/devproc/30-work.md"

echo "== p1: task data is project data"
mkdir -p "$P1/.claude/devproc/tasks/2026-10-24-x/run"; echo "D-01" > "$P1/.claude/devproc/tasks/2026-10-24-x/DECISIONS.md"

echo "== p1: uninstall dry run writes nothing"
before="$(state "$P1")"
uninst "$P1" > "$T/ud.log"
check "uninstall dry run: nothing changed" test "$before" = "$(state "$P1")"

echo "== p1: uninstall"
uninst --apply "$P1" > "$T/u1.log" || { cat "$T/u1.log"; bad "uninstall exit"; }
check "unchanged rule removed" test ! -e "$P1/.claude/rules/devproc/00-precedence.md"
check "evolved rule kept" test -f "$P1/.claude/rules/devproc/30-work.md"
check "task data kept" test -f "$P1/.claude/devproc/tasks/2026-10-24-x/DECISIONS.md"
check "manifest removed" test ! -e "$P1/.claude/devproc/manifest.json"
check "plugin snapshots removed" test ! -e "$P1/.claude/devproc/dist"
check "exclude kept for kept task data" grep -qx "/.claude/devproc/" "$P1/.git/info/exclude"
check "exclude kept for evolved rule" grep -qx "/.claude/rules/devproc/" "$P1/.git/info/exclude"
check "kept files not committable" test -z "$(git -C "$P1" status --porcelain)"
check "uninstall lists kept task file" grep -q "tasks/2026-10-24-x/DECISIONS.md" "$T/u1.log"
check "uninstall does not list snapshot files as kept" lacks "$T/u1.log" "dist/"
check "settings.local.json removed (devproc created it)" test ! -e "$P1/.claude/settings.local.json"
check "plugin record gone from registry" lacks_rec "$P1ID" "$P1"
check "marketplace gone from registry" reg_lacks "${P1ID#devproc@}"

echo "== p2: non-git directory"
inst --apply "$P2" > "$T/i3.log" || { cat "$T/i3.log"; bad "p2 install exit"; }
check "p2: note says not a git work tree" grep -q "not a git work tree" "$T/i3.log"
uninst --apply "$P2" > "$T/u3.log"
check "p2: directory empty again" test -z "$(ls -A "$P2")"

echo "== p3: existing files are never overwritten"
p3set="$(sha256sum < "$P3/.claude/settings.local.json")"
inst --apply "$P3" > "$T/i4.log" || { cat "$T/i4.log"; bad "p3 install exit"; }
check "p3: own rule untouched" grep -qx "# my own rules" "$P3/.claude/rules/devproc/10-modes.md"
check "p3: own rule reported" grep -q "10-modes.md exists and differs" "$T/i4.log"
check "p3: existing allow rule kept" grep -q 'Bash(ls \*)' "$P3/.claude/settings.local.json"
uninst --apply "$P3" > "$T/u4.log"
check "p3: own rule still there after uninstall" grep -qx "# my own rules" "$P3/.claude/rules/devproc/10-modes.md"
check "p3: adopted identical file kept" test -f "$P3/.claude/rules/devproc/00-precedence.md"
check "p3: pre-existing dir kept" test -d "$P3/.claude/rules/devproc"
check "p3: pre-existing settings file kept" grep -q 'Bash(ls \*)' "$P3/.claude/settings.local.json"
check "p3: pre-existing settings file byte-identical to before install" test "$p3set" = "$(sha256sum < "$P3/.claude/settings.local.json")"

echo "== refusals"
mkdir -p "$T/fakehome"   # never the real home: a broken check would install into it
HOME="$T/fakehome" check "refuses \$HOME" refused "$T/fakehome" "refusing $(realpath "$T/fakehome"): devproc is per project"
check "refuses /" refused / "refusing /: devproc is per project"
check "refuses source dir" refused "$SRC" "refusing to install into the devproc source itself"
check "refuses missing dir" refused "$T/nope" "target is not a directory"
ln -s "$SRC" "$T/srclink"
set +e; "$T/srclink/install.sh" --apply "$T/srclink" > "$T/refuse.log" 2>&1; rc=$?; set -e
check "refuses the source dir when run through a symlink to it" test "$rc" = 1 -a -n "$(grep 'devproc source itself' "$T/refuse.log")"
set +e; "$SRC/install.sh" > "$T/usage.log" 2>&1; rc=$?; python3 -I "$SRC/lib/devproc_install.py" frobnicate "$P2" >> "$T/usage.log" 2>&1; rc2=$?; set -e
check "usage: no directory or a bad command exits 2 with usage" test "$rc $rc2" = "2 2" -a -n "$(grep usage: "$T/usage.log")"
mkdir -p "$T/noclaude"; ln -s "$(command -v git)" "$T/noclaude/git"
set +e; PATH="$T/noclaude" "$(command -v python3)" -I "$SRC/lib/devproc_install.py" status "$P2" > "$T/noclaude.log" 2>&1; rc=$?; set -e
check "claude missing: clean message, exit 1" test "$rc" = 1 -a -n "$(grep 'claude. not found on PATH' "$T/noclaude.log")"

echo "== rollback: plugin install fails half way"
P4="$T/p4"; mkdir -p "$P4"; git -C "$P4" init -q
fake_fail plugin install
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P4" > "$T/rb.log" 2>&1 && bad "rollback: install should fail" || true
check "rollback: reported" grep -q "rolled back" "$T/rb.log"
check "rollback: no rules left" test ! -e "$P4/.claude/rules/devproc/00-precedence.md"
check "rollback: no manifest" test ! -e "$P4/.claude/devproc/manifest.json"
check "rollback: exclude restored" lacks "$P4/.git/info/exclude" "added by devproc"
check "rollback: marketplace removed" bash -c "! grep -q devproc '$P4/.claude/settings.local.json' 2>/dev/null"
check "rollback: no settings.local.json left" test ! -e "$P4/.claude/settings.local.json"
check "rollback: no .claude dir left" test ! -e "$P4/.claude"
check "rollback: marketplace not registered" reg_lacks "$(mname "$P4")"
check "rollback: exit 1" test "$(set +e; PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P4" >/dev/null 2>&1; echo $?)" = 1

echo "== rollback: marketplace add does its work, then fails (the failed step itself is undone)"
fake_fail after plugin marketplace add
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P4" > "$T/rba.log" 2>&1 && bad "partial step: install should fail" || true
check "partial step: rollback complete" grep -q "nothing from this run is left" "$T/rba.log"
check "partial step: marketplace not left registered" reg_lacks "$(mname "$P4")"
check "partial step: nothing left in the directory" test ! -e "$P4/.claude"

echo "== rollback: the snapshot fails validation half way (its own partial copy is removed)"
fake_fail plugin validate "$P4/.claude/devproc/dist"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P4" > "$T/rbv.log" 2>&1 && bad "snapshot rollback: install should fail" || true
check "snapshot rollback: complete" grep -q "nothing from this run is left" "$T/rbv.log"
check "snapshot rollback: no .partial copy left" test ! -e "$P4/.claude"

echo "== two directories: removing one leaves the other working"
P5="$T/p5"; P6="$T/p6"; mkdir -p "$P5" "$P6"; git -C "$P5" init -q; git -C "$P6" init -q
inst --apply "$P5" > "$T/i5.log" || { cat "$T/i5.log"; bad "p5 install exit"; }
inst --apply "$P6" > "$T/i6.log" || { cat "$T/i6.log"; bad "p6 install exit"; }
check "p5 and p6 have different marketplaces" test "$(mf "$P5" marketplace)" != "$(mf "$P6" marketplace)"
uninst --apply "$P5" > "$T/u5.log"
check "p6: still enabled after p5 uninstall" grep -q "\"$(mf "$P6" pluginId)\": true" "$P6/.claude/settings.local.json"
ip="$(loaded "$P6")"
check "p6: claude plugin list shows it" test -n "$ip"
check "p6: loads from its own snapshot" test "$ip" = "$P6/.claude/devproc/dist/$(mf "$P6" version)/plugin"
check "p6: snapshot guard still present" test -f "$ip/scripts/guard.py"

echo "== upgrade: a code change reaches an installed directory only via --upgrade"
setver 9.9.9
chmod u+w "$SRC/plugin/scripts/guard.py"; echo "# e2e-marker" >> "$SRC/plugin/scripts/guard.py"
check "before upgrade: loaded copy unchanged" lacks "$ip/scripts/guard.py" "e2e-marker"
inst --upgrade --apply "$P6" > "$T/up6.log" || { cat "$T/up6.log"; bad "p6 upgrade exit"; }
ip2="$(loaded "$P6")"
check "after upgrade: loads the 9.9.9 snapshot" test "$ip2" = "$P6/.claude/devproc/dist/9.9.9/plugin"
check "after upgrade: loaded copy has the change" grep -q e2e-marker "$ip2/scripts/guard.py"
check "after upgrade: manifest version 9.9.9" grep -q '"version": "9.9.9"' "$P6/.claude/devproc/manifest.json"
check "after upgrade: previous snapshot kept for running sessions" test -d "$ip"
echo "== upgrade rollback: plugin update fails after a rule was updated"
echo "- new template line" >> "$SRC/plugin/templates/rules/20-safety.md"
setver 9.9.10
rule_before="$(sha256sum < "$P6/.claude/rules/devproc/20-safety.md")"; man_before="$(sha256sum < "$P6/.claude/devproc/manifest.json")"
fake_fail once plugin update
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --upgrade --apply "$P6" > "$T/uprb.log" 2>&1 && bad "upgrade rollback: should fail" || true
check "upgrade rollback: reported" grep -q "rolled back; nothing from this run is left" "$T/uprb.log"
check "upgrade rollback: rule restored, not deleted" test "$rule_before" = "$(sha256sum < "$P6/.claude/rules/devproc/20-safety.md")"
check "upgrade rollback: manifest unchanged" test "$man_before" = "$(sha256sum < "$P6/.claude/devproc/manifest.json")"
check "upgrade rollback: still loads 9.9.9" test "$(loaded "$P6")" = "$P6/.claude/devproc/dist/9.9.9/plugin"
check "upgrade rollback: new snapshot removed" test ! -e "$P6/.claude/devproc/dist/9.9.10"
check "upgrade rollback: status healthy (exit 0)" test "$(status_rc "$P6")" = 0

echo "== uninstall stops on failure and a rerun finishes it"
fake_fail plugin marketplace remove
PATH="$T/fakebin:$PATH" "$SRC/uninstall.sh" --apply "$P6" > "$T/ufail.log" 2>&1 && bad "uninstall: should stop" || true
check "uninstall failure: says safe to rerun" grep -q "safe to rerun" "$T/ufail.log"
check "uninstall failure: no false rollback claim" lacks "$T/ufail.log" "nothing from this run is left"
check "uninstall failure: exit 3" test "$(set +e; PATH="$T/fakebin:$PATH" "$SRC/uninstall.sh" --apply "$P6" >/dev/null 2>&1; echo $?)" = 3
uninst --apply "$P6" > "$T/u6.log" || { cat "$T/u6.log"; bad "uninstall rerun exit"; }
check "p6: removed cleanly" test ! -e "$P6/.claude/devproc/manifest.json"
check "p6: no exclude lines left (nothing kept)" lacks "$P6/.git/info/exclude" "added by devproc"
check "p6: plugin gone from list" list_lacks_dir "$P6"
check "p6: directory empty again (only .git)" test "$(ls -A "$P6")" = ".git"

echo "== rollback while another directory is installed leaves it working"
P8="$T/p8"; P9="$T/p9"; mkdir -p "$P8" "$P9"; git -C "$P8" init -q; git -C "$P9" init -q
inst --apply "$P8" > "$T/i8.log" || { cat "$T/i8.log"; bad "p8 install exit"; }
fake_fail plugin install
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P9" > "$T/rb9.log" 2>&1 && bad "p9: install should fail" || true
check "p9 rollback: complete" grep -q "nothing from this run is left" "$T/rb9.log"
check "p8: still loads its snapshot after p9 rollback" test "$(loaded "$P8")" = "$P8/.claude/devproc/dist/9.9.10/plugin"
check "p8: marketplace still registered" reg_has "$(mf "$P8" marketplace)"
check "p8: status healthy" test "$(status_rc "$P8")" = 0
uninst --apply "$P8" > "$T/u8.log"

echo "== per-directory snapshots: what a session loads"
P12="$T/p12"; P13="$T/p13"; mkdir -p "$P12" "$P13"; git -C "$P12" init -q; git -C "$P13" init -q
inst --apply "$P12" > "$T/i12.log" || { cat "$T/i12.log"; bad "p12 install exit"; }
D12="$P12/.claude/devproc/dist/9.9.10"
check "p12: snapshot is read-only" test -z "$(writable_in "$D12")"
check "p12: plugin list loads the snapshot" test "$(loaded "$P12")" = "$D12/plugin"
check "p12: a real session reads the snapshot's hooks.json" test "$(session_hooks "$P12")" = "$D12/plugin/hooks/hooks.json"
check "p12: status healthy (exit 0)" test "$(status_rc "$P12")" = 0
echo "-- the source changes, same version"
cp "$SRC/plugin/scripts/guard.py" "$T/guard.saved"
echo "# src-edit" >> "$SRC/plugin/scripts/guard.py"
python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["description"]="edited"; open(p,"w").write(json.dumps(d))' "$SRC/.claude-plugin/marketplace.json"
check "source edit does not reach the snapshot" lacks "$D12/plugin/scripts/guard.py" "src-edit"
check "source edit: session still reads the snapshot" test "$(session_hooks "$P12")" = "$D12/plugin/hooks/hooks.json"
check "source edit: status still healthy" test "$(status_rc "$P12")" = 0
check "source edit: status says the source differs under the same version" grep -q "differs from the installed 9.9.10 under the same version" "$T/status.log"
before="$(state "$P12")"
inst --upgrade --apply "$P12" > "$T/sv.log" 2>&1 && bad "same version, changed source: upgrade should refuse" || true
check "same version, changed source: upgrade refused" grep -q "A version's code never changes" "$T/sv.log"
check "same version, changed source: nothing changed" test "$before" = "$(state "$P12")"
echo "-- two directories on different versions"
setver 9.9.20
inst --apply "$P13" > "$T/i13.log" || { cat "$T/i13.log"; bad "p13 install exit"; }
D13="$P13/.claude/devproc/dist/9.9.20"
check "p13: loads its own 9.9.20 snapshot" test "$(session_hooks "$P13")" = "$D13/plugin/hooks/hooks.json"
check "p13: snapshot has the source edit" grep -q src-edit "$D13/plugin/scripts/guard.py"
check "p12: still loads 9.9.10" test "$(session_hooks "$P12")" = "$D12/plugin/hooks/hooks.json"
echo "-- upgrading one directory leaves the other alone"
inst --upgrade --apply "$P12" > "$T/up12.log" || { cat "$T/up12.log"; bad "p12 upgrade exit"; }
check "p12: now loads 9.9.20" test "$(session_hooks "$P12")" = "$P12/.claude/devproc/dist/9.9.20/plugin/hooks/hooks.json"
check "p13: unaffected by p12 upgrade" test "$(loaded "$P13")" = "$D13/plugin"
check "p13: status healthy" test "$(status_rc "$P13")" = 0
setver 9.9.21
inst --upgrade --apply "$P12" > "$T/up12b.log" || { cat "$T/up12b.log"; bad "p12 second upgrade exit"; }
check "p12: loads 9.9.21" test "$(loaded "$P12")" = "$P12/.claude/devproc/dist/9.9.21/plugin"
check "p12: previous snapshot 9.9.20 kept" test -d "$P12/.claude/devproc/dist/9.9.20"
check "p12: older snapshot 9.9.10 pruned" test ! -e "$D12"
inst --upgrade --apply "$P12" > "$T/up12c.log" || { cat "$T/up12c.log"; bad "p12 same-version re-upgrade exit"; }
check "same-version re-upgrade: says unchanged" grep -q "plugin already at 9.9.21 with the same code" "$T/up12c.log"
check "same-version re-upgrade: previous snapshot 9.9.20 still kept" test -d "$P12/.claude/devproc/dist/9.9.20"
echo "-- damaged snapshot"
chmod u+w "$D13/plugin/scripts/guard.py" "$D13/plugin/scripts"; echo "# tampered" >> "$D13/plugin/scripts/guard.py"
check "damaged: status exit 4" test "$(status_rc "$P13")" = 4
check "damaged: status names the problem" grep -q "PROBLEM: the plugin snapshot .* changed since install" "$T/status.log"
check "damaged: status at another source version suggests --upgrade" grep -q "PROBLEM: .* Fix: install.sh --upgrade --apply" "$T/status.log"
inst --upgrade "$P13" > "$T/dmg-up.log"
check "damaged: an upgrade would fix it (dry run)" grep -q "problem, fixed by this run: the plugin snapshot" "$T/dmg-up.log"
inst --repair --apply "$P13" > "$T/dmg-r1.log" 2>&1 && bad "damaged: repair from a 9.9.21 source should refuse" || true
check "damaged: repair refused when the source is at another version" grep -q "Repair restores 9.9.20 exactly" "$T/dmg-r1.log"
setver 9.9.20; echo "# other change" >> "$SRC/plugin/scripts/guard.py"
inst --repair --apply "$P13" > "$T/dmg-r2.log" 2>&1 && bad "damaged: repair from a changed source should refuse" || true
check "damaged: repair refused when the source code differs" grep -q "repair cannot restore it" "$T/dmg-r2.log"
cp "$T/guard.saved" "$SRC/plugin/scripts/guard.py"; echo "# src-edit" >> "$SRC/plugin/scripts/guard.py"
before="$(state "$P13")"
inst --repair "$P13" > "$T/dmg-dry.log"
check "repair dry run writes nothing" test "$before" = "$(state "$P13")"
inst --repair --apply "$P13" > "$T/dmg-r3.log" || { cat "$T/dmg-r3.log"; bad "damaged: repair exit"; }
check "repair dry run listed every step the repair did" same_steps "$T/dmg-dry.log" "$T/dmg-r3.log"
check "damaged: repaired, status healthy" test "$(status_rc "$P13")" = 0
check "damaged: tamper gone" lacks "$D13/plugin/scripts/guard.py" "tampered"
check "damaged: snapshot read-only again" test -z "$(writable_in "$D13")"
check "damaged: no broken copy left" test "$(ls "$P13/.claude/devproc/dist")" = 9.9.20
echo "-- tampered marketplace.json in the snapshot"
MK13="$D13/.claude-plugin/marketplace.json"
chmod u+w "$MK13"; python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["owner"]["name"]="tampered"; open(p,"w").write(json.dumps(d))' "$MK13"; chmod u-w "$MK13"
check "tampered marketplace.json: status exit 4" test "$(status_rc "$P13")" = 4
check "tampered marketplace.json: status names the snapshot" grep -q "PROBLEM: the plugin snapshot .* changed since install" "$T/status.log"
inst --repair --apply "$P13" > "$T/mk.log" || { cat "$T/mk.log"; bad "tampered marketplace.json: repair exit"; }
check "tampered marketplace.json: repaired, status healthy" test "$(status_rc "$P13")" = 0
check "tampered marketplace.json: tamper gone" lacks "$MK13" "tampered"
echo "-- a symlink added inside the snapshot"
chmod u+w "$D13/plugin"; ln -s /etc "$D13/plugin/extra"; chmod u-w "$D13/plugin"
check "symlink in snapshot: status exit 4" test "$(status_rc "$P13")" = 4
inst --repair --apply "$P13" > "$T/sl.log" || { cat "$T/sl.log"; bad "symlink in snapshot: repair exit"; }
check "symlink in snapshot: repaired, status healthy" test "$(status_rc "$P13")" = 0
check "symlink in snapshot: link gone" test ! -L "$D13/plugin/extra"
echo "-- missing snapshot"
chmod -R u+w "$D13"; rm -rf "$D13"
check "missing: status exit 4" test "$(status_rc "$P13")" = 4
inst --repair --apply "$P13" > "$T/miss.log" || { cat "$T/miss.log"; bad "missing: repair exit"; }
check "missing: repaired, status healthy" test "$(status_rc "$P13")" = 0
check "missing: session reads the snapshot again" test "$(session_hooks "$P13")" = "$D13/plugin/hooks/hooks.json"
echo "-- plugin disabled in this directory"
(cd "$P13" && claude plugin disable "$(mf "$P13" pluginId)" --scope local) > /dev/null 2>&1
check "disabled: status exit 4" test "$(status_rc "$P13")" = 4
inst --repair --apply "$P13" > "$T/dis.log" || { cat "$T/dis.log"; bad "disabled: repair exit"; }
check "disabled: repaired, status healthy" test "$(status_rc "$P13")" = 0
echo "-- moved project"
P13M="$T/p13moved"; mv "$P13" "$P13M"
check "moved: status exit 4" test "$(status_rc "$P13M")" = 4
check "moved: status says moved or copied" grep -q "moved or copied from $P13" "$T/status.log"
M13OLD="$(mf "$P13M" marketplace)"
session_hooks "$P13M" > /dev/null || true
check "moved: a session there before the repair left a record of the old plugin id" has_rec "devproc@$M13OLD" "$P13M"
inst --repair --apply "$P13M" > "$T/mv.log" || { cat "$T/mv.log"; bad "moved: repair exit"; }
check "moved: repaired, status healthy" test "$(status_rc "$P13M")" = 0
check "moved: gets a marketplace named for its new place" test "$(mf "$P13M" marketplace)" = "$(mname "$P13M")"
check "moved: the old folder's dead registration removed" reg_lacks "$M13OLD"
check "moved: the record of the old plugin id removed" lacks_rec "devproc@$M13OLD" "$P13M"
check "moved: session reads the snapshot at the new place" test "$(session_hooks "$P13M")" = "$P13M/.claude/devproc/dist/9.9.20/plugin/hooks/hooks.json"
echo "-- copied project (original still there)"
P13C="$T/p13copy"; cp -a "$P13M" "$P13C"
mkdir -p "$P13C/build"; session_hooks "$P13C/build" > /dev/null || true; rm -rf "$P13C/build"   # a record whose folder is gone
P13ID="$(mf "$P13M" pluginId)"
check "copy: status exit 4" test "$(status_rc "$P13C")" = 4
check "copy: before repair, a session there loads the ORIGINAL's snapshot" test "$(session_hooks "$P13C")" = "$P13M/.claude/devproc/dist/9.9.20/plugin/hooks/hooks.json"
check "copy: that session left a record for the copy" has_rec "$P13ID" "$P13C"
before="$(state "$P13C")"
fake_fail after plugin install "devproc@$(mname "$P13C")"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --repair --apply "$P13C" > "$T/cprb.log" 2>&1 && bad "copy repair rollback: should fail" || true
check "copy repair rollback: complete" grep -q "rolled back; nothing from this run is left" "$T/cprb.log"
check "copy repair rollback: directory and registry as before (the copy's old record included)" test "$before" = "$(state "$P13C")"
inst --repair --apply "$P13C" > "$T/cp.log" || { cat "$T/cp.log"; bad "copy: repair exit"; }
check "copy: gets its own marketplace" test "$(mf "$P13C" marketplace)" != "$(mf "$P13M" marketplace)"
check "copy: its record of the original's plugin removed" lacks_rec "$P13ID" "$P13C"
check "copy: exclude file recorded is the copy's own" test "$(mf "$P13C" exclude | grep -o "$P13C/.git/info/exclude")" = "$P13C/.git/info/exclude"
check "copy: status healthy" test "$(status_rc "$P13C")" = 0
check "copy: loads its own snapshot" test "$(loaded "$P13C")" = "$P13C/.claude/devproc/dist/9.9.20/plugin"
check "original: still healthy after the copy's repair" test "$(status_rc "$P13M")" = 0
check "original: still loads its own snapshot" test "$(loaded "$P13M")" = "$P13M/.claude/devproc/dist/9.9.20/plugin"
uninst --apply "$P13C" > "$T/ucp.log" || { cat "$T/ucp.log"; bad "copy: uninstall exit"; }
check "original: healthy after the copy's uninstall" test "$(status_rc "$P13M")" = 0
echo "-- copied project, never repaired: its uninstall leaves the original alone"
P13U="$T/p13unrepaired"; cp -a "$P13M" "$P13U"
ex13="$(sha256sum < "$P13M/.git/info/exclude")"
session_hooks "$P13U" > /dev/null || true
check "unrepaired copy: a session there left a record for the copy" has_rec "$P13ID" "$P13U"
mkdir -p "$P13U/sub"; session_hooks "$P13U/sub" > /dev/null || true
uninst --apply "$P13U" > "$T/ucu.log" || { cat "$T/ucu.log"; bad "unrepaired copy: uninstall exit"; }
check "unrepaired copy uninstall: says the marketplace stays registered" grep -q "left registered" "$T/ucu.log"
check "unrepaired copy uninstall: original's exclude file untouched" test "$ex13" = "$(sha256sum < "$P13M/.git/info/exclude")"
check "unrepaired copy uninstall: original's git status clean" test -z "$(git -C "$P13M" status --porcelain)"
check "unrepaired copy uninstall: original healthy" test "$(status_rc "$P13M")" = 0
check "unrepaired copy uninstall: copy's records removed (subdirectory too)" no_recs_at "$P13U"
check "unrepaired copy uninstall: copy's subdirectory record removed" no_recs_at "$P13U/sub"
check "unrepaired copy uninstall: copy's own exclude lines removed" lacks "$P13U/.git/info/exclude" "added by devproc"
rmdir "$P13U/sub"
check "unrepaired copy uninstall: copy empty again (only .git)" test "$(ls -A "$P13U")" = ".git"
echo "-- moved project, never repaired: uninstall still removes its marketplace"
P16="$T/p16"; mkdir -p "$P16"; git -C "$P16" init -q
inst --apply "$P16" > "$T/i16.log" || { cat "$T/i16.log"; bad "p16 install exit"; }
M16="$(mf "$P16" marketplace)"; P16M="$T/p16moved"; mv "$P16" "$P16M"
uninst --apply "$P16M" > "$T/u16.log" || { cat "$T/u16.log"; bad "unrepaired move: uninstall exit"; }
check "unrepaired move: marketplace removed" reg_lacks "$M16"
check "unrepaired move: no records left" no_recs_at "$P16"
check "unrepaired move: no exclude lines left" lacks "$P16M/.git/info/exclude" "added by devproc"
check "unrepaired move: empty again (only .git)" test "$(ls -A "$P16M")" = ".git"
echo "-- marketplace name taken over by another folder (everything else intact)"
P13X="$T/p13hijack"; cp -a "$P13M" "$P13X"
(cd "$P13X" && claude plugin marketplace add "$P13X/.claude/devproc/dist/9.9.20" --scope local) > /dev/null 2>&1
check "hijacked: status exit 4" test "$(status_rc "$P13M")" = 4
check "hijacked: status says it points elsewhere" grep -q "points at $P13X" "$T/status.log"
before="$(state "$P13M")"
fake_fail after plugin marketplace add "$P13M"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --repair --apply "$P13M" > "$T/hjrb.log" 2>&1 && bad "hijack repair rollback: should fail" || true
check "hijack repair rollback: complete" grep -q "rolled back; nothing from this run is left" "$T/hjrb.log"
check "hijack repair rollback: registry points at the other folder again, as before" test "$before" = "$(state "$P13M")"
inst --repair --apply "$P13M" > "$T/hj.log" || { cat "$T/hj.log"; bad "hijacked: repair exit"; }
check "hijacked: repaired, loads its own snapshot again" test "$(loaded "$P13M")" = "$P13M/.claude/devproc/dist/9.9.20/plugin"
check "hijacked: status healthy" test "$(status_rc "$P13M")" = 0
chmod -R u+w "$P13X"; rm -rf "$P13X"
echo "-- the devproc lines removed from the git exclude file"
cp "$P13M/.git/info/exclude" "$T/ex13.saved"; grep -v "devproc" "$T/ex13.saved" > "$P13M/.git/info/exclude"
check "not ignored: status exit 4" test "$(status_rc "$P13M")" = 4
check "not ignored: status names the problem" grep -q "PROBLEM: .claude/devproc/ is not ignored by git" "$T/status.log"
inst --repair --apply "$P13M" > "$T/ex.log" || { cat "$T/ex.log"; bad "not ignored: repair exit"; }
check "not ignored: repaired, status healthy" test "$(status_rc "$P13M")" = 0
check "not ignored: devproc's own paths hidden again" test -z "$(git -C "$P13M" status --porcelain -uall | grep -v '^?? .claude/rules/')"
check "not ignored: the rules folder line not forced back (a team may commit the rules)" test -n "$(git -C "$P13M" status --porcelain -uall | grep '^?? .claude/rules/')"
cp "$T/ex13.saved" "$P13M/.git/info/exclude"
echo "-- repair fails half way and rolls back to the damaged state"
D13M="$P13M/.claude/devproc/dist/9.9.20"
chmod u+w "$D13M/plugin/scripts/guard.py"; echo "# tampered" >> "$D13M/plugin/scripts/guard.py"   # left writable: the rollback must keep modes too
before="$(state "$P13M")"
fake_fail plugin validate "$D13M"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --repair --apply "$P13M" > "$T/rprb.log" 2>&1 && bad "repair rollback: should fail" || true
check "repair rollback: complete" grep -q "rolled back; nothing from this run is left" "$T/rprb.log"
check "repair rollback: directory and registry as before" test "$before" = "$(state "$P13M")"
inst --repair --apply "$P13M" > "$T/rp2.log" || { cat "$T/rp2.log"; bad "repair after rollback exit"; }
check "repair after rollback: healthy" test "$(status_rc "$P13M")" = 0
echo "-- upgrade dry run writes nothing; going back to the kept previous version reuses its snapshot"
before="$(state "$P12")"
inst --upgrade "$P12" > "$T/updry.log"
check "upgrade dry run writes nothing" test "$before" = "$(state "$P12")"
inst --upgrade --apply "$P12" > "$T/upback.log" || { cat "$T/upback.log"; bad "p12 back to 9.9.20 exit"; }
check "back to 9.9.20: kept snapshot reused" grep -q "kept snapshot .* matches this source: reused" "$T/upback.log"
check "back to 9.9.20: loads it" test "$(loaded "$P12")" = "$P12/.claude/devproc/dist/9.9.20/plugin"
check "back to 9.9.20: status healthy" test "$(status_rc "$P12")" = 0
echo "-- a stray file and a symlink in dist/: removed, the link's target untouched"
mkdir -p "$T/outside"; echo keep > "$T/outside/f"; chmod 555 "$T/outside"
echo x > "$P12/.claude/devproc/dist/stray.txt"; ln -s "$T/outside" "$P12/.claude/devproc/dist/link"
uninst --apply "$P12" > "$T/u12.log" || { cat "$T/u12.log"; bad "p12 uninstall exit"; }
check "stray entries: dist/ removed" test ! -e "$P12/.claude/devproc/dist"
check "stray entries: the link's target kept, mode unchanged" test -f "$T/outside/f" -a "$(stat -c %a "$T/outside")" = 555
uninst --apply "$P13M" > "$T/u13.log" || { cat "$T/u13.log"; bad "p13 uninstall exit"; }
check "p12, p13: snapshots removed" test ! -e "$P12/.claude/devproc/dist" -a ! -e "$P13M/.claude/devproc/dist"

echo "== migration from a 0.2.0 install (shared marketplace, plugin run from the clone)"
OLD="${OLD_SRC:-}"
if [ -z "$OLD" ] && git -C "$REAL_SRC" rev-parse -q --verify refs/tags/v0.2.0 >/dev/null 2>&1; then
  mkdir -p "$T/old"; git -C "$REAL_SRC" archive v0.2.0 | tar -x -C "$T/old"; OLD="$T/old"
fi
if [ -z "$OLD" ]; then
  bad "migration checks SKIPPED: set OLD_SRC to the devproc 0.2.0 source"
else
  cp -r "$OLD" "$T/oldsrc"; OLD="$T/oldsrc"
  P14="$T/p14"; P15="$T/p15"; mkdir -p "$P14" "$P15"; git -C "$P14" init -q; git -C "$P15" init -q
  "$OLD/install.sh" --apply "$P14" > "$T/o14.log" || { cat "$T/o14.log"; bad "0.2.0 install p14"; }
  "$OLD/install.sh" --apply "$P15" > "$T/o15.log" || { cat "$T/o15.log"; bad "0.2.0 install p15"; }
  echo "- p14 rule edit" >> "$P14/.claude/rules/devproc/30-work.md"
  mkdir -p "$P14/sub"; session_hooks "$P14/sub" > /dev/null || true
  check "0.2.0 install: a session in a subdirectory left a record of its own" has_rec devproc@devproc "$P14/sub"
  inst --status "$P14" > "$T/o-st.log"
  check "0.2.0 install: status names the old layout" grep -q "installed with the 0.2.0 layout" "$T/o-st.log"
  inst --repair --apply "$P14" > "$T/o-rep.log" 2>&1 && bad "0.2.0 install: repair should refuse" || true
  check "0.2.0 install: repair refused, points to --upgrade" grep -q "run --upgrade" "$T/o-rep.log"
  inst --upgrade --apply "$P14" > "$T/m14.log" || { cat "$T/m14.log"; bad "migrate p14 exit"; }
  check "migrated p14: own snapshot loaded" test "$(session_hooks "$P14")" = "$P14/.claude/devproc/dist/9.9.20/plugin/hooks/hooks.json"
  check "migrated p14: status healthy" test "$(status_rc "$P14")" = 0
  check "migrated p14: evolved rule kept" grep -q "p14 rule edit" "$P14/.claude/rules/devproc/30-work.md"
  check "migrated p14: old plugin id gone from its settings" lacks "$P14/.claude/settings.local.json" "\"devproc@devproc\""
  check "migrated p14: its old record removed" lacks_rec devproc@devproc "$P14"
  check "migrated p14: its subdirectory's old record removed" lacks_rec devproc@devproc "$P14/sub"
  check "p15 (still 0.2.0): shared marketplace kept" reg_has devproc
  check "p15 (still 0.2.0): still loads from the 0.2.0 source" test "$(session_hooks "$P15")" = "$OLD/plugin/hooks/hooks.json"
  inst --upgrade --apply "$P15" > "$T/m15.log" || { cat "$T/m15.log"; bad "migrate p15 exit"; }
  check "migrated p15: status healthy" test "$(status_rc "$P15")" = 0
  check "last 0.2.0 directory migrated: shared marketplace removed" reg_lacks devproc
  check "migrated p14: unaffected by p15 migration" test "$(status_rc "$P14")" = 0
  uninst --apply "$P14" > "$T/u14.log" || { cat "$T/u14.log"; bad "p14 uninstall exit"; }
  uninst --apply "$P15" > "$T/u15.log" || { cat "$T/u15.log"; bad "p15 uninstall exit"; }
  rmdir "$P14/sub"
  check "p14: after uninstall only its evolved rule is left" test "$(cd "$P14" && find . -path ./.git -prune -o -type f -print)" = "./.claude/rules/devproc/30-work.md"
  echo "-- 0.2.0 installs removed by this uninstaller"
  P17="$T/p17"; P18="$T/p18"; mkdir -p "$P17" "$P18"; git -C "$P17" init -q; git -C "$P18" init -q
  ex17="$(sha256sum < "$P17/.git/info/exclude")"; ex18="$(sha256sum < "$P18/.git/info/exclude")"
  "$OLD/install.sh" --apply "$P17" > "$T/o17.log" || { cat "$T/o17.log"; bad "0.2.0 install p17"; }
  "$OLD/install.sh" --apply "$P18" > "$T/o18.log" || { cat "$T/o18.log"; bad "0.2.0 install p18"; }
  uninst --apply "$P17" > "$T/u17.log" || { cat "$T/u17.log"; bad "p17 uninstall exit"; }
  check "0.2.0 p17 removed: empty again (only .git)" test "$(ls -A "$P17")" = ".git"
  check "0.2.0 p17 removed: exclude file as before install" test "$ex17" = "$(sha256sum < "$P17/.git/info/exclude")"
  check "0.2.0 p17 removed: its record gone" lacks_rec devproc@devproc "$P17"
  check "0.2.0 p17 removed: shared marketplace kept for p18" reg_has devproc
  check "0.2.0 p18: still loads from the 0.2.0 source" test "$(session_hooks "$P18")" = "$OLD/plugin/hooks/hooks.json"
  uninst --apply "$P18" > "$T/u18.log" || { cat "$T/u18.log"; bad "p18 uninstall exit"; }
  check "last 0.2.0 install removed: shared marketplace removed" reg_lacks devproc
  check "0.2.0 p18 removed: empty again (only .git)" test "$(ls -A "$P18")" = ".git"
  check "0.2.0 p18 removed: exclude file as before install" test "$ex18" = "$(sha256sum < "$P18/.git/info/exclude")"
  echo "-- migration fails half way and rolls back to the 0.2.0 install"
  P19="$T/p19"; mkdir -p "$P19"; git -C "$P19" init -q; ex19="$(sha256sum < "$P19/.git/info/exclude")"
  "$OLD/install.sh" --apply "$P19" > "$T/o19.log" || { cat "$T/o19.log"; bad "0.2.0 install p19"; }
  before="$(snap "$P19")"
  fake_fail plugin install "$(mname "$P19" | sed 's/^/devproc@/')"
  PATH="$T/fakebin:$PATH" "$SRC/install.sh" --upgrade --apply "$P19" > "$T/m19.log" 2>&1 && bad "migration rollback: should fail" || true
  check "migration rollback: complete" grep -q "rolled back; nothing from this run is left" "$T/m19.log"
  check "migration rollback: directory as before" test "$before" = "$(snap "$P19")"
  check "migration rollback: new marketplace not registered" reg_lacks "$(mname "$P19")"
  check "migration rollback: still loads from the 0.2.0 source" test "$(session_hooks "$P19")" = "$OLD/plugin/hooks/hooks.json"
  check "migration rollback: status says 0.2.0 layout, enabled (exit 0)" test "$(status_rc "$P19")" = 0
  echo "-- a 0.2.0 install whose plugin the project had before devproc: migration refuses"
  python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["pluginInstalledByDevproc"]=False; open(p,"w").write(json.dumps(d))' "$P19/.claude/devproc/manifest.json"
  before="$(state "$P19")"
  inst --upgrade --apply "$P19" > "$T/m19b.log" 2>&1 && bad "not ours: migration should refuse" || true
  check "not ours: migration refused" grep -q "devproc did not install it" "$T/m19b.log"
  check "not ours: nothing changed" test "$before" = "$(state "$P19")"
  python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["pluginInstalledByDevproc"]=True; open(p,"w").write(json.dumps(d))' "$P19/.claude/devproc/manifest.json"
  uninst --apply "$P19" > "$T/u19.log" || { cat "$T/u19.log"; bad "p19 uninstall exit"; }
  check "p19: empty again (only .git)" test "$(ls -A "$P19")" = ".git"
  check "p19: exclude file as before install" test "$ex19" = "$(sha256sum < "$P19/.git/info/exclude")"
  echo "-- a 0.2.0 install nested inside another 0.2.0 install: migrating the outer one leaves the inner alone"
  P34="$T/p34"; mkdir -p "$P34/inner"; git -C "$P34/inner" init -q   # outer: not a git repo; inner: its own repo
  "$OLD/install.sh" --apply "$P34" > "$T/o34.log" || { cat "$T/o34.log"; bad "0.2.0 install p34"; }
  "$OLD/install.sh" --apply "$P34/inner" > "$T/o34i.log" || { cat "$T/o34i.log"; bad "0.2.0 install p34/inner"; }
  inst --upgrade --apply "$P34" > "$T/m34.log" || { cat "$T/m34.log"; bad "migrate p34 exit"; }
  check "nested: the inner 0.2.0 install keeps its record" has_rec devproc@devproc "$P34/inner"
  check "nested: the shared marketplace kept for it" reg_has devproc
  check "nested: the inner one still loads from the 0.2.0 source" test "$(session_hooks "$P34/inner")" = "$OLD/plugin/hooks/hooks.json"
  uninst --apply "$P34/inner" > "$T/u34i.log" || { cat "$T/u34i.log"; bad "p34 inner uninstall exit"; }
  uninst --apply "$P34" > "$T/u34.log" || { cat "$T/u34.log"; bad "p34 uninstall exit"; }
  check "nested: both removed, shared marketplace gone" reg_lacks devproc
  echo "-- a 0.2.0 install on an exclude file without a newline at its end (0.2.0 joined its marker to the last pattern)"
  P30="$T/p30"; mkdir -p "$P30"; git -C "$P30" init -q; printf '*.log' > "$P30/.git/info/exclude"
  "$OLD/install.sh" --apply "$P30" > "$T/o30.log" || { cat "$T/o30.log"; bad "0.2.0 install p30"; }
  check "0.2.0 joined its marker: the project's pattern is broken" bash -c "! git -C '$P30' check-ignore -q x.log"
  inst --upgrade --apply "$P30" > "$T/m30.log" || { cat "$T/m30.log"; bad "migrate p30 exit"; }
  check "migrated p30: the project's pattern works again" git -C "$P30" check-ignore -q x.log
  check "migrated p30: status healthy" test "$(status_rc "$P30")" = 0
  uninst --apply "$P30" > "$T/u30.log" || { cat "$T/u30.log"; bad "p30 uninstall exit"; }
  check "p30 removed: the pattern still works, no devproc line left" test -z "$(grep -v '^\*\.log$' "$P30/.git/info/exclude")" -a -n "$(grep -x '\*\.log' "$P30/.git/info/exclude")"
fi

echo "== one run at a time per directory"
P22="$T/p22"; mkdir -p "$P22"; git -C "$P22" init -q
python3 -I -c 'import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX); open(sys.argv[2], "w").close(); time.sleep(120)' "$P22" "$T/locked" &
lockpid=$!
for _ in $(seq 100); do [ -e "$T/locked" ] && break; sleep 0.1; done
set +e; inst --apply "$P22" > "$T/lock.log" 2>&1; rc=$?; set -e
kill "$lockpid"; wait "$lockpid" 2>/dev/null || true
check "locked: refused with exit 1" test "$rc" = 1
check "locked: says another run is working" grep -q "another devproc run is working on" "$T/lock.log"
check "locked: nothing written" test ! -e "$P22/.claude"

echo "== a damaged manifest: a clean message, no traceback"
inst --apply "$P22" > "$T/i22.log" || { cat "$T/i22.log"; bad "p22 install exit"; }
cp "$P22/.claude/devproc/manifest.json" "$T/m22.saved"
echo '{broken' > "$P22/.claude/devproc/manifest.json"
check "broken manifest: status exit 1" test "$(status_rc "$P22")" = 1
check "broken manifest: says not valid JSON" grep -q "manifest.json is not valid JSON" "$T/status.log"
check "broken manifest: no traceback" lacks "$T/status.log" "Traceback"
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); del d["pluginHash"]; open(sys.argv[2],"w").write(json.dumps(d))' "$T/m22.saved" "$P22/.claude/devproc/manifest.json"
set +e; uninst --apply "$P22" > "$T/u22b.log" 2>&1; rc=$?; set -e
check "manifest lacking a key: uninstall refuses with exit 1" test "$rc" = 1
check "manifest lacking a key: names the key" grep -q "lacks pluginHash" "$T/u22b.log"
cp "$T/m22.saved" "$P22/.claude/devproc/manifest.json"
uninst --apply "$P22" > "$T/u22.log" || { cat "$T/u22.log"; bad "p22 uninstall exit"; }
check "p22: empty again (only .git)" test "$(ls -A "$P22")" = ".git"

echo "== subdirectory of a repo: refused"
P11="$T/p11"; mkdir -p "$P11/pkg"; git -C "$P11" init -q
"$SRC/install.sh" --apply "$P11/pkg" > "$T/i11.log" 2>&1 && bad "subdir: install should refuse" || true
check "subdir: refusal names the repo top" grep -q "install there instead: $P11" "$T/i11.log"
check "subdir: nothing written" test ! -e "$P11/pkg/.claude" -a ! -e "$P11/.claude"

echo "== linked git worktree: refused, nothing written (Claude Code keeps local settings in the main checkout)"
P7="$T/p7"; mkdir -p "$P7"; git -C "$P7" init -q
git -C "$P7" -c user.name=t -c user.email=t@t commit -q --allow-empty -m base
git -C "$P7" worktree add -q "$T/p7wt" -b wt
before7="$(snap "$P7"; snap "$T/p7wt")"
"$SRC/install.sh" --apply "$T/p7wt" > "$T/i7.log" 2>&1 && bad "worktree: install should refuse" || true
check "worktree: refusal names the main checkout" grep -q "install there instead: $P7" "$T/i7.log"
check "worktree: nothing written" test "$before7" = "$(snap "$P7"; snap "$T/p7wt")"
check "worktree: main checkout still installs" "$SRC/install.sh" --apply "$P7"
mkdir -p "$P7/sub/deep"
check "a session in a subdirectory loads this directory's snapshot" test "$(session_hooks "$P7/sub/deep")" = "$P7/.claude/devproc/dist/$(mf "$P7" version)/plugin/hooks/hooks.json"
check "worktree: main checkout uninstalls" "$SRC/uninstall.sh" --apply "$P7"

echo "== git exclude file without a newline at its end"
P24="$T/p24"; mkdir -p "$P24"; git -C "$P24" init -q; printf '*.log' > "$P24/.git/info/exclude"
inst --apply "$P24" > "$T/i24.log" || { cat "$T/i24.log"; bad "p24 install exit"; }
check "no final newline: the project's last pattern still works" git -C "$P24" check-ignore -q x.log
check "no final newline: devproc paths ignored" git -C "$P24" check-ignore -q .claude/devproc/manifest.json
MONO2="$T/mono2"; mkdir -p "$MONO2"; git -C "$MONO2" init -q; printf '/build/\n' > "$MONO2/.git/info/exclude"
(cd "$P24" && tar --exclude=.git -cf - .) | (mkdir -p "$MONO2/sub" && cd "$MONO2/sub" && tar -xf -)
uninst --apply "$MONO2/sub" > "$T/u24c.log" || { cat "$T/u24c.log"; bad "p24 copy uninstall exit"; }
check "no final newline, copy in another repo: that repo's exclude file byte-identical" test "$(od -c < "$MONO2/.git/info/exclude")" = "$(printf '/build/\n' | od -c)"
uninst --apply "$P24" > "$T/u24.log" || { cat "$T/u24.log"; bad "p24 uninstall exit"; }
check "no final newline: exclude file byte-identical after uninstall" test "$(cat "$P24/.git/info/exclude")" = '*.log' -a "$(wc -c < "$P24/.git/info/exclude")" = 5

echo "== failed install with an existing settings.local.json: restored to its exact bytes"
P25="$T/p25"; mkdir -p "$P25/.claude"; git -C "$P25" init -q
printf '{"permissions":{"allow":["Bash(ls *)"]}}\r\n' > "$P25/.claude/settings.local.json"
before="$(state "$P25")"
fake_fail plugin install
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P25" > "$T/rb25.log" 2>&1 && bad "p25: install should fail" || true
check "p25 rollback: complete" grep -q "nothing from this run is left" "$T/rb25.log"
check "p25 rollback: directory (settings bytes included) and registry as before" test "$before" = "$(state "$P25")"
inst --apply "$P25" > "$T/i25.log" || { cat "$T/i25.log"; bad "p25 install exit"; }
uninst --apply "$P25" > "$T/u25.log" || { cat "$T/u25.log"; bad "p25 uninstall exit"; }
check "p25: CRLF settings file byte-identical after install and uninstall" test "$before" = "$(state "$P25")"

echo "== wrong-typed files: a clean message, no traceback"
P26="$T/p26"; mkdir -p "$P26/.claude"; git -C "$P26" init -q; echo '[]' > "$P26/.claude/settings.local.json"
set +e; inst --apply "$P26" > "$T/i26.log" 2>&1; rc=$?; set -e
check "settings.local.json not an object: install refused with exit 1" test "$rc" = 1 -a -n "$(grep 'is not a JSON object' "$T/i26.log")"
check "settings.local.json not an object: nothing written" test ! -e "$P26/.claude/devproc"
rm "$P26/.claude/settings.local.json"
inst --apply "$P26" > "$T/i26b.log" || { cat "$T/i26b.log"; bad "p26 install exit"; }
cp "$P26/.claude/devproc/manifest.json" "$T/m26.saved"
for bad_manifest in 'null' '[]' "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); d["layout"]="2"; print(json.dumps(d))' "$T/m26.saved")" \
    "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); d["files"]=[]; print(json.dumps(d))' "$T/m26.saved")"; do
  printf '%s' "$bad_manifest" > "$P26/.claude/devproc/manifest.json"
  rc="$(status_rc "$P26")"
  check "manifest ${bad_manifest:0:20}...: status exit 1, no traceback" test "$rc" = 1 -a -z "$(grep Traceback "$T/status.log")" -a -n "$(grep 'manifest.json' "$T/status.log")"
done
cp "$T/m26.saved" "$P26/.claude/devproc/manifest.json"

echo "== an installed directory copied into another repo (without its .git)"
MONO="$T/mono"; mkdir -p "$MONO"; git -C "$MONO" init -q
printf '/build/\n' > "$MONO/.git/info/exclude"
(cd "$P26" && tar --exclude=.git -cf - .) | (mkdir -p "$MONO/sub" && cd "$MONO/sub" && tar -xf -)
monoex="$(sha256sum < "$MONO/.git/info/exclude")"
set +e; inst --repair --apply "$MONO/sub" > "$T/mono-r.log" 2>&1; rc=$?; inst --upgrade --apply "$MONO/sub" > "$T/mono-u.log" 2>&1; rc2=$?; set -e
check "inside another repo: repair and upgrade refused, naming the repo top" test "$rc $rc2" = "1 1" -a -n "$(grep "install there instead: $MONO" "$T/mono-r.log")"
uninst --apply "$MONO/sub" > "$T/mono-un.log" || { cat "$T/mono-un.log"; bad "inside another repo: uninstall exit"; }
check "inside another repo: uninstall leaves that repo's exclude file alone" test "$monoex" = "$(sha256sum < "$MONO/.git/info/exclude")"
check "inside another repo: nothing of devproc left there (.claude was there before)" test -d "$MONO/sub/.claude" -a -z "$(ls -A "$MONO/sub/.claude")"
check "inside another repo: the original still healthy" test "$(status_rc "$P26")" = 0
uninst --apply "$P26" > "$T/u26.log" || { cat "$T/u26.log"; bad "p26 uninstall exit"; }

echo "== a moved directory and a newer source: upgrade moves it and fixes the move"
P27="$T/p27"; mkdir -p "$P27"; git -C "$P27" init -q
inst --apply "$P27" > "$T/i27.log" || { cat "$T/i27.log"; bad "p27 install exit"; }
M27="$(mf "$P27" marketplace)"; P27M="$T/p27moved"; mv "$P27" "$P27M"
setver 9.9.30
check "moved, newer source: status suggests --upgrade" test "$(status_rc "$P27M")" = 4 -a -n "$(grep 'Fix: install.sh --upgrade' "$T/status.log")"
set +e; inst --repair --apply "$P27M" > "$T/r27.log" 2>&1; rc=$?; set -e
check "moved, newer source: repair refused, points to --upgrade" test "$rc" = 1 -a -n "$(grep 'Or run --upgrade' "$T/r27.log")"
inst --upgrade --apply "$P27M" > "$T/up27.log" || { cat "$T/up27.log"; bad "p27 upgrade exit"; }
check "moved, newer source: upgraded, status healthy" test "$(status_rc "$P27M")" = 0
check "moved, newer source: loads its 9.9.30 snapshot at the new place" test "$(session_hooks "$P27M")" = "$P27M/.claude/devproc/dist/9.9.30/plugin/hooks/hooks.json"
check "moved, newer source: the old folder's dead registration removed" reg_lacks "$M27"
check "moved, newer source: snapshots other than 9.9.30 removed" test "$(ls "$P27M/.claude/devproc/dist")" = 9.9.30
uninst --apply "$P27M" > "$T/u27.log" || { cat "$T/u27.log"; bad "p27 uninstall exit"; }
check "p27: empty again (only .git)" test "$(ls -A "$P27M")" = ".git"

echo "== a tracked settings.local.json: install refused"
P28="$T/p28"; mkdir -p "$P28/.claude"; git -C "$P28" init -q; echo '{}' > "$P28/.claude/settings.local.json"; git -C "$P28" add -f .claude/settings.local.json
before="$(state "$P28")"
check "tracked settings.local.json: install refused" refused "$P28" "is tracked by git"
check "tracked settings.local.json: nothing changed" test "$before" = "$(state "$P28")"

echo "== the rules folder shared with a team, as docs/02 describes: still healthy"
P31="$T/p31"; mkdir -p "$P31"; git -C "$P31" init -q
inst --apply "$P31" > "$T/i31.log" || { cat "$T/i31.log"; bad "p31 install exit"; }
python3 -I -c '
import sys
p, mark = sys.argv[1], sys.argv[2]
l = open(p).read().split("\n")
out, i = [], 0
while i < len(l):
    if l[i] == mark and i + 1 < len(l) and l[i + 1] == "/.claude/rules/devproc/":
        i += 2; continue
    out.append(l[i]); i += 1
open(p, "w").write("\n".join(out))' "$P31/.git/info/exclude" "# devproc: added by devproc install.sh, removed by uninstall.sh"
git -C "$P31" add .claude/rules/devproc
check "rules committed: status healthy (exit 0)" test "$(status_rc "$P31")" = 0
check "rules committed: repair has nothing to do" bash -c "'$SRC/install.sh' --repair --apply '$P31' | grep -q 'nothing to repair'"
git -C "$P31" rm -rq --cached .claude/rules/devproc
uninst --apply "$P31" > "$T/u31.log" || { cat "$T/u31.log"; bad "p31 uninstall exit"; }

echo "== marketplace name taken by a live folder, then the original moved: the live registration stays"
P29="$T/p29"; mkdir -p "$P29"; git -C "$P29" init -q
inst --apply "$P29" > "$T/i29.log" || { cat "$T/i29.log"; bad "p29 install exit"; }
M29="$(mf "$P29" marketplace)"; P29X="$T/p29other"; cp -a "$P29" "$P29X"
(cd "$P29X" && claude plugin marketplace add "$P29X/.claude/devproc/dist/$(mf "$P29" version)" --scope local) > /dev/null 2>&1
P29M="$T/p29moved"; mv "$P29" "$P29M"
inst --repair --apply "$P29M" > "$T/r29.log" || { cat "$T/r29.log"; bad "p29 moved repair exit"; }
check "p29 moved: repaired, status healthy" test "$(status_rc "$P29M")" = 0
check "p29 moved: the other folder's live registration of the old name kept" test "$(registered | grep "^$M29 " | cut -d' ' -f2-)" = "$P29X/.claude/devproc/dist/$(mf "$P29M" version)"
uninst --apply "$P29M" > "$T/u29.log" || { cat "$T/u29.log"; bad "p29 uninstall exit"; }
uninst --apply "$P29X" > "$T/u29x.log" || { cat "$T/u29x.log"; bad "p29 other uninstall exit"; }
check "p29 other folder: its own registration removed on uninstall" reg_lacks "$M29"

echo "== a copy whose original still exists but lost its snapshot: the copy's repair keeps the original's registration"
P35="$T/p35"; mkdir -p "$P35"; git -C "$P35" init -q
inst --apply "$P35" > "$T/i35.log" || { cat "$T/i35.log"; bad "p35 install exit"; }
M35="$(mf "$P35" marketplace)"; P35C="$T/p35copy"; cp -a "$P35" "$P35C"
chmod -R u+w "$P35/.claude/devproc/dist"; rm -rf "$P35/.claude/devproc/dist"
inst --repair --apply "$P35C" > "$T/r35c.log" || { cat "$T/r35c.log"; bad "p35 copy repair exit"; }
check "original alive, snapshot gone: its registration kept by the copy's repair" reg_has "$M35"
inst --repair --apply "$P35" > "$T/r35.log" || { cat "$T/r35.log"; bad "p35 repair exit"; }
check "original repaired afterwards: healthy" test "$(status_rc "$P35")" = 0
uninst --apply "$P35C" > "$T/u35c.log" || { cat "$T/u35c.log"; bad "p35 copy uninstall exit"; }
uninst --apply "$P35" > "$T/u35.log" || { cat "$T/u35.log"; bad "p35 uninstall exit"; }

echo "== unsafe layouts: refused before anything is written"
P32="$T/p32"; mkdir -p "$P32/.claude" "$T/elsewhere"; git -C "$P32" init -q; ln -s "$T/elsewhere" "$P32/.claude/devproc"
before="$(state "$P32")"
check "symlinked .claude/devproc: install refused" refused "$P32" "is a symlink"
check "symlinked .claude/devproc: nothing written, here or where it points" test "$before" = "$(state "$P32")" -a -z "$(ls -A "$T/elsewhere")"
P33="$T/p33"; mkdir -p "$P33"; git -C "$P33" init -q
inst --apply "$P33" > "$T/i33.log" || { cat "$T/i33.log"; bad "p33 install exit"; }
git -C "$P33" add -f .claude/settings.local.json
set +e; inst --repair --apply "$P33" > "$T/r33.log" 2>&1; rc=$?; inst --upgrade --apply "$P33" > "$T/up33.log" 2>&1; rc2=$?; set -e
check "settings.local.json tracked after install: repair and upgrade refused" test "$rc $rc2" = "1 1" -a -n "$(grep 'is tracked by git' "$T/up33.log")"
check "settings.local.json tracked: status names the manual fix" test "$(status_rc "$P33")" = 4 -a -n "$(grep "Fix by hand: untrack it: git -C $P33 rm --cached" "$T/status.log")"
git -C "$P33" rm -q --cached .claude/settings.local.json
uninst --apply "$P33" > "$T/u33.log" || { cat "$T/u33.log"; bad "p33 uninstall exit"; }
PB="$T/pbare"; git init -q --bare "$PB.git"; git -C "$PB.git" worktree add -q "$PB-wt" -b w 2>/dev/null || true   # needs git 2.42+ on an empty bare repo
check "worktree of a bare repo: refused" refused "$PB-wt" "whose main checkout git cannot name"

echo "== leftovers of a killed run (no manifest): install names each, with its command"
P36="$T/p36"; mkdir -p "$P36/.claude/devproc/dist/9.9.1/plugin"; git -C "$P36" init -q; chmod -R a-w "$P36/.claude/devproc/dist/9.9.1"
before="$(state "$P36")"
check "killed run leftovers: install refused" refused "$P36" "has devproc leftovers but no manifest"
check "killed run leftovers: the remove command is printed" grep -qF "chmod -R u+w $P36/.claude/devproc/dist/9.9.1 && rm -rf $P36/.claude/devproc/dist/9.9.1" "$T/refuse.log"
check "killed run leftovers: nothing changed" test "$before" = "$(state "$P36")"
chmod -R u+w "$P36/.claude/devproc/dist/9.9.1" && rm -rf "$P36/.claude/devproc/dist/9.9.1"

echo "== reinstall after an uninstall that kept task data: the kept exclude lines become devproc's again"
P39="$T/p39"; mkdir -p "$P39"; git -C "$P39" init -q; ex39="$(sha256sum < "$P39/.git/info/exclude")"
inst --apply "$P39" > "$T/i39.log" || { cat "$T/i39.log"; bad "p39 install exit"; }
mkdir -p "$P39/.claude/devproc/tasks/t"; echo x > "$P39/.claude/devproc/tasks/t/TASK.md"
uninst --apply "$P39" > "$T/u39.log" || { cat "$T/u39.log"; bad "p39 uninstall exit"; }
inst --apply "$P39" > "$T/i39b.log" || { cat "$T/i39b.log"; bad "p39 reinstall exit"; }
rm -rf "$P39/.claude/devproc/tasks"
uninst --apply "$P39" > "$T/u39b.log" || { cat "$T/u39b.log"; bad "p39 second uninstall exit"; }
check "reinstall: after the last uninstall the exclude file is as before the first install" test "$ex39" = "$(sha256sum < "$P39/.git/info/exclude")"

echo "== a broken .git file: a git error, not \"no repo\""
P38="$T/p38"; mkdir -p "$P38"; git -C "$P38" init -q
inst --apply "$P38" > "$T/i38.log" || { cat "$T/i38.log"; bad "p38 install exit"; }
mv "$P38/.git" "$T/p38.git"; echo "gitdir: $T/nowhere/.git" > "$P38/.git"
check "broken .git file: status exit 4, names the git error" test "$(status_rc "$P38")" = 4 -a -n "$(grep 'could not be checked: git cannot read' "$T/status.log")"
P38B="$T/p38b"; mkdir -p "$P38B"; echo "gitdir: $T/nowhere/.git" > "$P38B/.git"
check "broken .git file: install refused" refused "$P38B" "git cannot read the repository"
rm "$P38/.git"; mv "$T/p38.git" "$P38/.git"
uninst --apply "$P38" > "$T/u38.log" || { cat "$T/u38.log"; bad "p38 uninstall exit"; }

echo "== SIGHUP (terminal closed) rolls back; under nohup the run goes on"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
case "\$*" in "plugin install"*) touch "$T/in-install"; sleep 8 ;; esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"
P40="$T/p40"; mkdir -p "$P40"; git -C "$P40" init -q
before="$(state "$P40")"; rm -f "$T/in-install"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P40" > "$T/hup.log" 2>&1 & hpid=$!
for _ in $(seq 600); do [ -e "$T/in-install" ] && break; sleep 0.2; done
kill -HUP "$hpid"; set +e; wait "$hpid"; rc=$?; set -e
check "SIGHUP: exit 1 (rolled back)" test "$rc" = 1
check "SIGHUP: directory and registry as before" test "$before" = "$(state "$P40")"
rm -f "$T/in-install"
PATH="$T/fakebin:$PATH" nohup "$SRC/install.sh" --apply "$P40" > "$T/nohup.log" 2>&1 & hpid=$!
for _ in $(seq 600); do [ -e "$T/in-install" ] && break; sleep 0.2; done
kill -HUP "$hpid"; set +e; wait "$hpid"; rc=$?; set -e
check "nohup: run completes despite SIGHUP" test "$rc" = 0 -a "$(status_rc "$P40")" = 0
uninst --apply "$P40" > "$T/u40.log" || { cat "$T/u40.log"; bad "p40 uninstall exit"; }

wait_for() { for _ in $(seq 600); do [ -e "$1" ] && return 0; sleep 0.2; done; return 1; }
echo "== a closed terminal or Ctrl-C: the running claude call finishes; a second signal cannot cut the rollback short"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
case "\$*" in
  "plugin install"*) touch "$T/in-install"; sleep 8; "$real" "\$@"; rc=\$?; touch "$T/install-done"; exit \$rc ;;
  "plugin uninstall"*) touch "$T/in-undo"; sleep 3 ;;
esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"
# signals go to the whole process group, as a terminal sends them; setsid gives the run its own group
for sig in HUP INT; do
  P41="$T/p41-$sig"; mkdir -p "$P41"; git -C "$P41" init -q
  before="$(state "$P41")"; rm -f "$T/in-install" "$T/install-done" "$T/in-undo"
  # Ctrl-C reaches a foreground run only: a background job starts with SIGINT ignored, so reset it
  PATH="$T/fakebin:$PATH" setsid python3 -I -c 'import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])' \
    "$SRC/install.sh" --apply "$P41" > "$T/sig-$sig.log" 2>&1 & hpid=$!
  wait_for "$T/in-install" || bad "SIG$sig: plugin install never started"
  kill -"$sig" -- -"$hpid"
  wait_for "$T/in-undo" || bad "SIG$sig: the rollback never reached plugin uninstall"
  kill -"$sig" -- -"$hpid"
  set +e; wait "$hpid"; rc=$?; set -e
  check "SIG$sig: the running claude call was let finish" test -e "$T/install-done"
  check "SIG$sig: says it lets the running call finish" grep -qF 'interrupted: letting `claude plugin install' "$T/sig-$sig.log"
  check "SIG$sig twice: exit 1 (rolled back)" test "$rc" = 1
  check "SIG$sig twice: directory and registry as before" test "$before" = "$(state "$P41")"
done

echo "== a step fails, then SIGHUP arrives during its undo: the rollback is not cut short"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
case "\$*" in
  "plugin install"*) echo "simulated failure" >&2; exit 1 ;;
  "plugin uninstall"*) touch "$T/in-undo"; sleep 3 ;;
esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"
P47="$T/p47"; mkdir -p "$P47"; git -C "$P47" init -q
before="$(state "$P47")"; rm -f "$T/in-undo"
PATH="$T/fakebin:$PATH" setsid "$SRC/install.sh" --apply "$P47" > "$T/sig47.log" 2>&1 & hpid=$!
wait_for "$T/in-undo" || bad "p47: the rollback never reached plugin uninstall"
kill -HUP -- -"$hpid"; set +e; wait "$hpid"; rc=$?; set -e
check "failed step, then SIGHUP during the undo: exit 1 (rolled back)" test "$rc" = 1
check "failed step, then SIGHUP during the undo: directory and registry as before" test "$before" = "$(state "$P47")"

echo "== a hung claude call is stopped at the timeout, with what it started"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
# a grandchild in the call's process group, and one that left it (setsid): both hold the output
case "\$*" in "plugin install"*)
  sleep 300 & echo \$! > "$T/gc-group.pid"; setsid sleep 300 & echo \$! > "$T/gc-escaped.pid"; sleep 300 ;; esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"
P48="$T/p48"; mkdir -p "$P48"; git -C "$P48" init -q; before="$(state "$P48")"
t0=$SECONDS; set +e; DEVPROC_CLI_TIMEOUT=30 PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P48" > "$T/to48.log" 2>&1; rc=$?; set -e
took=$((SECONDS - t0)); kill -0 "$(cat "$T/gc-group.pid")" 2>/dev/null && left=1 || left=""
kill "$(cat "$T/gc-group.pid")" "$(cat "$T/gc-escaped.pid")" 2>/dev/null || true
check "timeout: the hung call is stopped and the run rolled back (exit 1)" test "$rc" = 1 -a "$before" = "$(state "$P48")"
check "timeout: says which call did not finish" grep -q '`claude plugin install.*did not finish within 30 s' "$T/to48.log"
check "timeout: done within 200 s, though an escaped grandchild still holds the output" test "$took" -lt 200
check "timeout: the hung call's own child processes are killed" test -z "$left"
for v in abc 0 86401 '²'; do
  set +e; DEVPROC_CLI_TIMEOUT="$v" inst --status "$P48" > "$T/tov.log" 2>&1; rc=$?; set -e
  check "DEVPROC_CLI_TIMEOUT=$v: exit 2, no traceback" test "$rc" = 2 -a -z "$(grep Traceback "$T/tov.log")"
done

echo "== interrupted while tidying up: the work is kept, nothing rolled back"
P49="$T/p49"; mkdir -p "$P49"; git -C "$P49" init -q
inst --apply "$P49" > /dev/null || bad "p49 install exit"
old49="$(mf "$P49" marketplace)"; mv "$P49" "$T/p49m"; P49="$T/p49m"; setver 9.9.49
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
case "\$*" in "plugin marketplace remove $old49"*) touch "$T/in-tidy"; sleep 3 ;; esac
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"; rm -f "$T/in-tidy"
PATH="$T/fakebin:$PATH" setsid "$SRC/install.sh" --upgrade --apply "$P49" > "$T/tidy49.log" 2>&1 & hpid=$!
wait_for "$T/in-tidy" || bad "p49: the upgrade never reached the dead registration's removal"
kill -HUP -- -"$hpid"; set +e; wait "$hpid"; rc=$?; set -e
check "interrupted while tidying: the upgrade is kept (exit 0, loads 9.9.49)" test "$rc" = 0 -a "$(loaded "$P49")" = "$P49/.claude/devproc/dist/9.9.49/plugin"
check "interrupted while tidying: says the remaining tidying was not done" grep -q "interrupted while tidying up" "$T/tidy49.log"
check "interrupted while tidying: names the step not done" grep -qF "not done: claude plugin marketplace remove $old49" "$T/tidy49.log"
check "interrupted while tidying: a tidying step already done is not listed" lacks "$T/tidy49.log" "not done: remove snapshots"
uninst --apply "$P49" > /dev/null 2>&1 || bad "p49 uninstall exit"
claude plugin marketplace remove "$old49" > /dev/null 2>&1 || true   # in case the interrupted call had not removed it

echo "== output pipe closed (| head): the run carries on"
P42="$T/p42"; mkdir -p "$P42"; git -C "$P42" init -q
( set +e; inst --apply "$P42" 2>&1; echo $? > "$T/pipe.rc" ) | head -n1 > /dev/null
check "pipe closed: install exit 0" test "$(cat "$T/pipe.rc")" = 0
check "pipe closed: installed and healthy" test "$(status_rc "$P42")" = 0
( set +e; uninst --apply "$P42" 2>&1; echo $? > "$T/pipe.rc" ) | head -n1 > /dev/null
check "pipe closed: uninstall exit 0, nothing left" test "$(cat "$T/pipe.rc")" = 0 -a -z "$(git -C "$P42" status --porcelain -uall)"

echo "== git answering in another language or as git 1.x; GIT_DIR set by the caller"
mkdir -p "$T/gitbin"; realgit="$(command -v git)"
cat > "$T/gitbin/git" <<EOF
#!/usr/bin/env bash
# real git, its "not a git repository" message translated unless LC_ALL=C, or capitalised as git 1.x
o="\$(mktemp)"; e="\$(mktemp)"; "$realgit" "\$@" > "\$o" 2> "\$e"; rc=\$?
cat "\$o"
if [ -n "\${GIT_STUB_OLD:-}" ]; then sed 's/not a git repository/Not a git repository/' "\$e" >&2
elif [ "\${LC_ALL:-}" != C ]; then sed 's/not a git repository/kein Git-Repository/' "\$e" >&2
else cat "\$e" >&2; fi
rm -f "\$o" "\$e"; exit \$rc
EOF
chmod +x "$T/gitbin/git"
for mode in translated old; do
  P43="$T/p43-$mode"; mkdir -p "$P43"     # not a git repo
  if [ "$mode" = old ]; then stub=(GIT_STUB_OLD=1); else stub=(-u LC_ALL); fi
  set +e; env "${stub[@]}" PATH="$T/gitbin:$PATH" "$SRC/install.sh" --apply "$P43" > "$T/i43.log" 2>&1; rc=$?; set -e
  check "git $mode: a non-git directory installs (exit 0)" test "$rc" = 0
  uninst --apply "$P43" > /dev/null 2>&1 || true
done
P43="$T/p43"; mkdir -p "$P43"; O43="$T/o43"; mkdir -p "$O43"; git -C "$O43" init -q; osnap="$(snap "$O43")"
GIT_DIR="$O43/.git" inst --apply "$P43" > "$T/i43.log" 2>&1 || { cat "$T/i43.log"; bad "GIT_DIR: install exit"; }
check "GIT_DIR from the caller: the other repository untouched" test "$osnap" = "$(snap "$O43")"
check "GIT_DIR from the caller: the target treated as the non-git folder it is" grep -q "not a git work tree" "$T/i43.log"
GIT_DIR="$O43/.git" uninst --apply "$P43" > /dev/null 2>&1 || bad "GIT_DIR: uninstall exit"
check "GIT_DIR from the caller: target empty again, the other repository untouched" test -z "$(ls -A "$P43")" -a "$osnap" = "$(snap "$O43")"

echo "== leftover remove command works for a path with a quote"
P44="$T/p4'4"; mkdir -p "$P44/.claude/devproc/dist/9.9.9/plugin"; chmod -R a-w "$P44/.claude/devproc/dist/9.9.9"
check "quoted path: install refused, naming the leftover" refused "$P44" "plugin snapshot"
cmd="$(sed -n 's/^ *plugin snapshot .*: \(chmod -R .*\)$/\1/p' "$T/refuse.log")"
bash -c "$cmd" > /dev/null 2>&1 || true
check "quoted path: the printed command removes the leftover" test -n "$cmd" -a ! -e "$P44/.claude/devproc/dist/9.9.9"

echo "== install over a 0.2.0 marker joined to the project's last pattern"
MARK="$(sed -n 's/^MARK = "\(.*\)"$/\1/p' "$SRC/lib/devproc_install.py")"
P45="$T/p45"; mkdir -p "$P45"; git -C "$P45" init -q
printf '*.log%s\n/.claude/devproc/\n' "$MARK" > "$P45/.git/info/exclude"
inst --apply "$P45" > "$T/i45.log" || { cat "$T/i45.log"; bad "p45 install exit"; }
check "joined marker: the project's pattern works while installed" git -C "$P45" check-ignore -q x.log
check "joined marker: one pair per devproc line" test "$(grep -cx '/.claude/devproc/' "$P45/.git/info/exclude")" = 1
uninst --apply "$P45" > /dev/null || bad "p45 uninstall exit"
check "joined marker: after uninstall only the project's pattern is left" test "$(cat "$P45/.git/info/exclude")" = '*.log'

echo "== a symlinked .claude leads to another directory's install: refused"
A46="$T/p46a"; B46="$T/p46b"; mkdir -p "$A46" "$B46"; git -C "$A46" init -q; git -C "$B46" init -q
inst --apply "$A46" > /dev/null || bad "p46a install exit"
ln -s "$A46/.claude" "$B46/.claude"; asnap="$(state "$A46")"
set +e; uninst --apply "$B46" > "$T/u46.log" 2>&1; rc=$?; set -e
check "symlinked .claude: uninstall refused (exit 1)" test "$rc" = 1
check "symlinked .claude: refusal names the symlink" grep -q "is a symlink" "$T/u46.log"
check "symlinked .claude: status exit 4" test "$(status_rc "$B46")" = 4
check "symlinked .claude: the other directory's install untouched" test "$asnap" = "$(state "$A46")"
rm "$B46/.claude"; uninst --apply "$A46" > /dev/null || bad "p46a uninstall exit"

echo "== nothing of devproc left in the registry"
check "no devproc marketplace registered" test -z "$(registered | grep '^devproc' || true)"
check "no devproc plugin records" no_recs

echo "installer-e2e: $fails failure(s)"
[ "$fails" -eq 0 ]
