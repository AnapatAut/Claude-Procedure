#!/usr/bin/env bash
# End-to-end test of install.sh / uninstall.sh in a sandbox.
# Uses a throwaway CLAUDE_CONFIG_DIR and throwaway target dirs under a temp dir:
# the real ~/.claude and real projects are never touched.
#   tests/installer-e2e.sh            exit 0 = every check passed
set -euo pipefail
REAL_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d -t devproc-e2e-XXXXXX)"
trap 'rm -rf "$T"' EXIT
# run against a throwaway copy, so the upgrade test can change the version
cp -r "$REAL_SRC" "$T/src"
SRC="$T/src"
export CLAUDE_CONFIG_DIR="$T/cfg"
mkdir -p "$CLAUDE_CONFIG_DIR"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
fails=0
ok()   { echo "ok   $*"; }
bad()  { echo "BAD  $*"; fails=$((fails+1)); }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
snap() { (cd "$1" && find . -path ./.git/objects -prune -o -type f -print0 | sort -z | xargs -0 sha256sum); cat "$1/.git/info/exclude" 2>/dev/null || true; }
inst() { "$SRC/install.sh" "$@"; }
uninst() { "$SRC/uninstall.sh" "$@"; }

# ---- targets
P1="$T/p1"; mkdir -p "$P1"; git -C "$P1" init -q
P2="$T/p2"; mkdir -p "$P2"                       # not a git repo
P3="$T/p3"; mkdir -p "$P3/.claude/rules/devproc"; git -C "$P3" init -q
echo "# my own rules" > "$P3/.claude/rules/devproc/10-modes.md"
cp "$SRC/plugin/templates/rules/00-precedence.md" "$P3/.claude/rules/devproc/00-precedence.md"
echo '{"permissions":{"allow":["Bash(ls *)"]}}' > "$P3/.claude/settings.local.json"

echo "== p1: dry run writes nothing"
before="$(snap "$P1")"
inst "$P1" > "$T/dry.log"
check "dry run: output says dry run" grep -q "dry run only" "$T/dry.log"
check "dry run: no file changed" test "$before" = "$(snap "$P1")"

echo "== p1: install"
inst --apply "$P1" > "$T/i1.log" || { cat "$T/i1.log"; bad "install exit"; }
check "rules seeded" test -f "$P1/.claude/rules/devproc/00-precedence.md"
check "config seeded" test -f "$P1/.claude/devproc/config.json"
check "MISTAKES seeded" test -f "$P1/.claude/devproc/MISTAKES.md"
check "manifest written" test -f "$P1/.claude/devproc/manifest.json"
check "plugin enabled locally" grep -q '"devproc@devproc": true' "$P1/.claude/settings.local.json"
check "git exclude has devproc block" grep -q "added by devproc" "$P1/.git/info/exclude"
check "git status clean (all ignored)" test -z "$(git -C "$P1" status --porcelain)"
check "post-check ran" grep -q "post-check: devproc@devproc .* enabled for this directory" "$T/i1.log"

echo "== p1: second install changes nothing"
before="$(snap "$P1")"
inst --apply "$P1" > "$T/i2.log"
check "second install: already installed" grep -q "already installed" "$T/i2.log"
check "second install: no file changed" test "$before" = "$(snap "$P1")"

echo "== p1: project evolves a rule; status + upgrade keep it"
echo "- project rule: always run e2e first" >> "$P1/.claude/rules/devproc/30-work.md"
inst --status "$P1" > "$T/st.log"
check "status: evolved file reported" grep -q "30-work.md.*evolved by project" "$T/st.log"
inst --upgrade --apply "$P1" > "$T/up.log" || { cat "$T/up.log"; bad "upgrade exit"; }
check "upgrade: evolved rule kept" grep -q "always run e2e first" "$P1/.claude/rules/devproc/30-work.md"

echo "== p1: task data is project data"
mkdir -p "$P1/.claude/devproc/tasks/2026-10-24-x/run"; echo "D-01" > "$P1/.claude/devproc/tasks/2026-10-24-x/DECISIONS.md"

echo "== p1: uninstall dry run writes nothing"
before="$(snap "$P1")"
uninst "$P1" > "$T/ud.log"
check "uninstall dry run: no file changed" test "$before" = "$(snap "$P1")"

echo "== p1: uninstall"
uninst --apply "$P1" > "$T/u1.log" || { cat "$T/u1.log"; bad "uninstall exit"; }
check "unchanged rule removed" test ! -e "$P1/.claude/rules/devproc/00-precedence.md"
check "evolved rule kept" test -f "$P1/.claude/rules/devproc/30-work.md"
check "task data kept" test -f "$P1/.claude/devproc/tasks/2026-10-24-x/DECISIONS.md"
check "manifest removed" test ! -e "$P1/.claude/devproc/manifest.json"
check "exclude kept for kept task data" grep -qx "/.claude/devproc/" "$P1/.git/info/exclude"
check "exclude kept for evolved rule" grep -qx "/.claude/rules/devproc/" "$P1/.git/info/exclude"
check "kept files not committable" test -z "$(git -C "$P1" status --porcelain)"
check "uninstall lists kept task file" grep -q "tasks/2026-10-24-x/DECISIONS.md" "$T/u1.log"
check "settings.local.json removed (devproc created it)" test ! -e "$P1/.claude/settings.local.json"
check "plugin gone from registry" bash -c "! grep -q devproc '$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json'"

echo "== p2: non-git directory"
inst --apply "$P2" > "$T/i3.log" || { cat "$T/i3.log"; bad "p2 install exit"; }
check "p2: note says not a git work tree" grep -q "not a git work tree" "$T/i3.log"
uninst --apply "$P2" > "$T/u3.log"
check "p2: directory empty again" test -z "$(ls -A "$P2")"

echo "== p3: existing files are never overwritten"
inst --apply "$P3" > "$T/i4.log" || { cat "$T/i4.log"; bad "p3 install exit"; }
check "p3: own rule untouched" grep -qx "# my own rules" "$P3/.claude/rules/devproc/10-modes.md"
check "p3: own rule reported" grep -q "10-modes.md exists and differs" "$T/i4.log"
check "p3: existing allow rule kept" grep -q 'Bash(ls \*)' "$P3/.claude/settings.local.json"
uninst --apply "$P3" > "$T/u4.log"
check "p3: own rule still there after uninstall" grep -qx "# my own rules" "$P3/.claude/rules/devproc/10-modes.md"
check "p3: adopted identical file kept" test -f "$P3/.claude/rules/devproc/00-precedence.md"
check "p3: pre-existing dir kept" test -d "$P3/.claude/rules/devproc"
check "p3: pre-existing settings file kept" grep -q 'Bash(ls \*)' "$P3/.claude/settings.local.json"

echo "== refusals"
check "refuses \$HOME" bash -c "! '$SRC/install.sh' '$HOME' >/dev/null"
check "refuses /" bash -c "! '$SRC/install.sh' / >/dev/null"
check "refuses source dir" bash -c "! '$SRC/install.sh' '$SRC' >/dev/null"
check "refuses missing dir" bash -c "! '$SRC/install.sh' '$T/nope' >/dev/null"

echo "== rollback: plugin install fails half way"
P4="$T/p4"; mkdir -p "$P4"; git -C "$P4" init -q
mkdir -p "$T/fakebin"; real="$(command -v claude)"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
if [ "\$1 \$2" = "plugin install" ]; then echo "simulated failure" >&2; exit 1; fi
exec "$real" "\$@"
EOF
chmod +x "$T/fakebin/claude"
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P4" > "$T/rb.log" 2>&1 && bad "rollback: install should fail" || true
check "rollback: reported" grep -q "rolled back" "$T/rb.log"
check "rollback: no rules left" test ! -e "$P4/.claude/rules/devproc/00-precedence.md"
check "rollback: no manifest" test ! -e "$P4/.claude/devproc/manifest.json"
check "rollback: exclude restored" bash -c "! grep -q 'added by devproc' '$P4/.git/info/exclude'"
check "rollback: marketplace removed" bash -c "! grep -q devproc '$P4/.claude/settings.local.json' 2>/dev/null"
check "rollback: no settings.local.json left" test ! -e "$P4/.claude/settings.local.json"
check "rollback: no .claude dir left" test ! -e "$P4/.claude"

echo "== two directories: removing one leaves the other working"
P5="$T/p5"; P6="$T/p6"; mkdir -p "$P5" "$P6"; git -C "$P5" init -q; git -C "$P6" init -q
inst --apply "$P5" > "$T/i5.log" || { cat "$T/i5.log"; bad "p5 install exit"; }
inst --apply "$P6" > "$T/i6.log" || { cat "$T/i6.log"; bad "p6 install exit"; }
uninst --apply "$P5" > "$T/u5.log"
check "p6: still enabled after p5 uninstall" grep -q '"devproc@devproc": true' "$P6/.claude/settings.local.json"
p6path() { (cd "$P6" && claude plugin list --json) | python3 -I -c '
import json, os, sys
d = os.path.realpath(sys.argv[1])
for p in json.load(sys.stdin):
    if p["id"] == "devproc@devproc" and p.get("enabled") and os.path.realpath(p.get("projectPath", "")) == d:
        print(p["installPath"]); break' "$P6"; }
ip="$(p6path)"
check "p6: claude plugin list shows it enabled" test -n "$ip"
check "p6: installed guard still present" test -f "$ip/scripts/guard.py"

echo "== upgrade: a code change reaches an installed directory only via --upgrade"
python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["version"]="9.9.9"; open(p,"w").write(json.dumps(d))' "$SRC/plugin/.claude-plugin/plugin.json"
echo "# e2e-marker" >> "$SRC/plugin/scripts/guard.py"
check "before upgrade: installed copy unchanged" bash -c "! grep -q e2e-marker '$ip/scripts/guard.py'"
inst --upgrade --apply "$P6" > "$T/up6.log" || { cat "$T/up6.log"; bad "p6 upgrade exit"; }
ip2="$(p6path)"
check "after upgrade: new version installed" bash -c "case '$ip2' in */9.9.9) exit 0;; *) exit 1;; esac"
check "after upgrade: installed copy has the change" grep -q e2e-marker "$ip2/scripts/guard.py"
check "after upgrade: manifest version 9.9.9" grep -q '"version": "9.9.9"' "$P6/.claude/devproc/manifest.json"
echo "== upgrade rollback: plugin update fails after a rule was updated"
echo "- new template line" >> "$SRC/plugin/templates/rules/20-safety.md"
python3 -I -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["version"]="9.9.10"; open(p,"w").write(json.dumps(d))' "$SRC/plugin/.claude-plugin/plugin.json"
rule_before="$(sha256sum < "$P6/.claude/rules/devproc/20-safety.md")"; man_before="$(sha256sum < "$P6/.claude/devproc/manifest.json")"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
if [ "\$1 \$2" = "plugin update" ]; then echo "simulated failure" >&2; exit 1; fi
exec "$real" "\$@"
EOF
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --upgrade --apply "$P6" > "$T/uprb.log" 2>&1 && bad "upgrade rollback: should fail" || true
check "upgrade rollback: reported" grep -q "rolled back" "$T/uprb.log"
check "upgrade rollback: rule restored, not deleted" test "$rule_before" = "$(sha256sum < "$P6/.claude/rules/devproc/20-safety.md")"
check "upgrade rollback: manifest unchanged" test "$man_before" = "$(sha256sum < "$P6/.claude/devproc/manifest.json")"
inst --status "$P6" > "$T/st6.log"
check "upgrade rollback: status not 'missing'" bash -c "! grep -q missing '$T/st6.log'"

echo "== uninstall stops on failure and a rerun finishes it"
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
if [ "\$1 \$2" = "plugin uninstall" ]; then echo "simulated failure" >&2; exit 1; fi
exec "$real" "\$@"
EOF
PATH="$T/fakebin:$PATH" "$SRC/uninstall.sh" --apply "$P6" > "$T/ufail.log" 2>&1 && bad "uninstall: should stop" || true
check "uninstall failure: says safe to rerun" grep -q "safe to rerun" "$T/ufail.log"
check "uninstall failure: no false rollback claim" bash -c "! grep -q 'nothing from this run is left' '$T/ufail.log'"
uninst --apply "$P6" > "$T/u6.log" || { cat "$T/u6.log"; bad "uninstall rerun exit"; }
check "p6: removed cleanly" test ! -e "$P6/.claude/devproc/manifest.json"
check "p6: no exclude lines left (nothing kept)" bash -c "! grep -q 'added by devproc' '$P6/.git/info/exclude'"
check "p6: plugin gone from list" bash -c "! (cd '$P6' && claude plugin list --json) | grep -q '$P6\"'"

echo "== rollback while another directory is installed leaves it working"
P8="$T/p8"; P9="$T/p9"; mkdir -p "$P8" "$P9"; git -C "$P8" init -q; git -C "$P9" init -q
inst --apply "$P8" > "$T/i8.log" || { cat "$T/i8.log"; bad "p8 install exit"; }
cat > "$T/fakebin/claude" <<EOF
#!/usr/bin/env bash
if [ "\$1 \$2" = "plugin install" ]; then echo "simulated failure" >&2; exit 1; fi
exec "$real" "\$@"
EOF
PATH="$T/fakebin:$PATH" "$SRC/install.sh" --apply "$P9" > "$T/rb9.log" 2>&1 && bad "p9: install should fail" || true
check "p9 rollback: complete" grep -q "nothing from this run is left" "$T/rb9.log"
check "p8: still listed enabled after p9 rollback" bash -c "(cd '$P8' && claude plugin list --json) | grep -q '$P8'"
check "p8: marketplace still registered" grep -q devproc "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json"
uninst --apply "$P8" > "$T/u8.log"

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
check "worktree: main checkout uninstalls" "$SRC/uninstall.sh" --apply "$P7"

echo "installer-e2e: $fails failure(s)"
[ "$fails" -eq 0 ]
