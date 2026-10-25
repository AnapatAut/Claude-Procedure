#!/usr/bin/env python3
"""Mutation test of the installer: proves tests/installer-e2e.sh catches each safeguard.

Each mutant removes one safeguard from lib/devproc_install.py in a throwaway copy of this
source; the e2e suite run on that copy must fail. A mutant whose run passes ("SURVIVED")
means no check covers that safeguard. The unmutated copy must pass first.
    tests/installer-mutants.py [-j N] [M01 M14 ...]
exit 0 = the unmutated run passed and every mutant was caught.
The migration checks need the devproc 0.2.0 source: set OLD_SRC, or run from a git clone
with the v0.2.0 tag. Runs take minutes each (they start real Claude Code sessions).
"""
import ast
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# name: (exact text in lib/devproc_install.py, replacement, the e2e check that must catch it:
# with E2E_FAIL_FAST the run stops at its first failed check, and that check must be this one)
MUTANTS = {
    'M01-same-version-refusal': ('    if layout >= LAYOUT and m["version"] == VERSION and phash != m["pluginHash"]:',
        '    if False:',
        'same version, changed source: upgrade should refuse'),
    'M02-snapshot-hash': ('        return tree_hash(snap) == plugin_hash and read_json(mkf) == mk',
        '        return read_json(mkf) == mk',
        'damaged: status exit 4'),
    'M03-readonly': ('        renamed = True\n        make_readonly(dst)',
        '        renamed = True',
        'p12: snapshot is read-only'),
    'M04-prune': ('    if not os.path.isdir(base) or os.path.islink(base):\n        return',
        '    if True:\n        return',
        'plugin snapshots removed'),
    'M05-relocated-detect': ('    relocated = target != m["projectPath"]',
        '    relocated = False',
        'moved: gets a marketplace named for its new place'),
    'M06-uninstall-marketplace-remove': ('        if reg is not None and ours:',
        '        if False:',
        'uninstall does not list snapshot files as kept'),
    'M07-migrate-shared-marketplace': ('    elif prev is not None:',
        '    elif False:',
        'last 0.2.0 directory migrated: shared marketplace removed'),
    'M08-repair-version': ('    if m["version"] != VERSION:\n        raise Fail("installed version %s, this source is at %s. Repair',
        '    if False:\n        raise Fail("installed version %s, this source is at %s. Repair',
        'damaged: repair refused when the source is at another version'),
    'M09-repair-hash': ('    if phash != m["pluginHash"]:\n        raise Fail("this source\'s plugin differs from what was installed',
        '    if False:\n        raise Fail("this source\'s plugin differs from what was installed',
        'damaged: repair from a changed source should refuse'),
    'M10-health-moved': ('    if target != m["projectPath"]:\n        out.append',
        '    if False:\n        out.append',
        'moved: status says moved or copied'),
    'M11-repoint-undo': ('            undo_add)',
        '            None)',
        'upgrade rollback: still loads 9.9.9'),
    'M12-health-registry': ('    elif not same_path(reg, dist):',
        '    elif False:',
        'hijacked: status exit 4'),
    'M13-health-enabled': ('    if not enabled(target, m["pluginId"]):\n        out.append',
        '    if False:\n        out.append',
        'disabled: status exit 4'),
    'M14-exclude-own-file': ('    f = git_exclude_file(target)\n    out = dict(ex, file=f, lines=lines)',
        '    f = ex["file"]\n    out = dict(ex, file=f, lines=lines)',
        "copy: exclude file recorded is the copy's own"),
    'M15-same-version-prune': ('        if new_version:  # same version: nothing to prune, a kept previous snapshot stays',
        '        if True:',
        'same-version re-upgrade: previous snapshot 9.9.20 still kept'),
    'M16-remove-symlink': ('    if os.path.islink(path) or not os.path.isdir(path):',
        '    if not os.path.isdir(path):',
        'p12 uninstall exit'),
    'M17-subdir-records': ('        if p.get("scope") == "local" and inside(d, target) and own_install(target, d):',
        '        if p.get("scope") == "local" and os.path.realpath(d) == target and own_install(target, d):',
        "migrated p14: its subdirectory's old record removed"),
    'M18-snapshot-marketplace-json': ('        return tree_hash(snap) == plugin_hash and read_json(mkf) == mk',
        '        return tree_hash(snap) == plugin_hash',
        'tampered marketplace.json: status exit 4'),
    'M19-src-realpath': ('os.path.dirname(os.path.dirname(os.path.realpath(__file__)))',
        'os.path.dirname(os.path.dirname(os.path.abspath(__file__)))',
        'refuses the source dir when run through a symlink to it'),
    'M20-settings-exact-restore': ('    if cur == was:',
        '    if False:',
        'p3: pre-existing settings file byte-identical to before install'),
    'M21-repair-move-back': ('move_aside, move_back)',
        'move_aside, None)',
        "copy repair rollback: directory and registry as before (the copy's old record included)"),
    'M22-migrate-ownership': ('    if not by_us:',
        '    if False:',
        'not ours: migration should refuse'),
    'M23-lock': ('            lock(target)',
        '            pass',
        'locked: refused with exit 1'),
    'M24-repair-copy-record': ('        for d in local_records(target, old_pid):',
        '        for d in []:',
        "copy: its record of the original's plugin removed"),
    'M25-uninstall-copy-record': ('            for d in local_records(target, pid):',
        '            for d in []:',
        "unrepaired copy uninstall: copy's records removed (subdirectory too)"),
    'M26-health-ignored': ('            elif not ignored(target, p):\n                out.append(("%s is not ignored',
        '            elif False:\n                out.append(("%s is not ignored',
        'not ignored: status exit 4'),
    'M27-manifest-keys': ('    if missing:\n        raise Fail(bad % ("lacks "',
        '    if False:\n        raise Fail(bad % ("lacks "',
        'manifest lacking a key: uninstall refuses with exit 1'),
    'M28-tool-check': ('            if not shutil.which(tool):',
        '            if False:',
        'claude missing: clean message, exit 1'),
    'M29-snapshot-partial-cleanup': ('    except BaseException:\n        remove_tree(tmp)\n',
        '    except BaseException:\n',
        'snapshot rollback: no .partial copy left'),
    'M30-migrate-settings-restore': ('        plan.add("save %s" % SETTINGS_LOCAL, save, restore)\n        if not os.path.isdir',
        '        plan.add("save %s" % SETTINGS_LOCAL, save, None)\n        if not os.path.isdir',
        'migration rollback: directory as before'),
    'M31-failed-step-undo': ('            for d, u in ([current] if current else []) + list(reversed(done)):',
        '            for d, u in list(reversed(done)):',
        'partial step: marketplace not left registered'),
    'M32-exclude-newline': ('            if nl:\n                f.write("\\n")',
        '            pass',
        "no final newline: the project's last pattern still works"),
    'M33-install-settings-restore': ('    plan.add("save %s" % SETTINGS_LOCAL, save, restore)  # restores its exact bytes',
        '    plan.add("save %s" % SETTINGS_LOCAL, save, None)',
        'p25 rollback: directory (settings bytes included) and registry as before'),
    'M34-manifest-object': ('    if not isinstance(m, dict):\n        raise Fail(bad % "is not a JSON object")',
        '    if False:\n        raise Fail(bad % "is not a JSON object")',
        'status exit 1, no traceback'),
    'M35-top-check-repair': ('top_only=args[0] in ("install", "upgrade", "repair")',
        'top_only=args[0] == "install"',
        'inside another repo: repair and upgrade refused, naming the repo top'),
    'M36-relocated-own-name': ('    name = marketplace_name(target) if relocated else old_name',
        '    name = old_name',
        'moved: repair exit'),
    'M37-dead-registration': ('        if original_gone and old_reg is not None and not os.path.isdir(old_reg):',
        '        if False:',
        "moved: the old folder's dead registration removed"),
    'M38-hash-symlinks': ('            if os.path.islink(p):\n                h.update(b"L\\0"',
        '            if os.path.islink(p):\n                continue\n                h.update(b"L\\0"',
        'symlink in snapshot: status exit 4'),
    'M40-original-gone': ('    original_gone = relocated and not os.path.exists(m["projectPath"])',
        '    original_gone = relocated',
        "original alive, snapshot gone: its registration kept by the copy's repair"),
    'M41-uninstall-ownership': ('        ours = reg is not None and (inside(reg, os.path.join(target, DIST)) or (',
        '        ours = reg is not None and (True or (',
        'unrepaired copy uninstall: says the marketplace stays registered'),
    'M42-relocated-record-undo': ('                plan.add(desc, do, lambda d=d: run(["claude", "plugin", "install", old_pid, "--scope", "local"], cwd=d))',
        '                plan.add(desc, do)',
        "copy repair rollback: directory and registry as before (the copy's old record included)"),
    'M43-repoint-back': ('            elif os.path.isdir(reg):\n                run(["claude", "plugin", "marketplace", "add", reg',
        '            elif False:\n                run(["claude", "plugin", "marketplace", "add", reg',
        'upgrade rollback: reported'),
    'M44-recorded-marketplace-json': ('    return mk if isinstance(mk, dict) else expected_marketplace(m["marketplace"])',
        '    return expected_marketplace(m["marketplace"])',
        'source edit: status still healthy'),
    'M39-settings-exclude-restore': ('    settings_will_stay = not m.get("settingsLocalExisted") and',
        '    settings_will_stay = True or',
        'p6: no exclude lines left (nothing kept)'),
    'M45-tracked-settings-refusal': ('    if git_top(target) and tracked(target, SETTINGS_LOCAL):',
        '    if False:',
        'tracked settings.local.json: install refused'),
    'M46-live-registration-kept': ('        if original_gone and old_reg is not None and not os.path.isdir(old_reg):',
        '        if original_gone and old_reg is not None:',
        "p29 moved: the other folder's live registration of the old name kept"),
    'M47-rules-not-required': ('    return [p for p in (".claude/devproc/", SETTINGS_LOCAL) if p == ".claude/devproc/" or p in lines]',
        '    return [p for p in EXCLUDE_PATHS if p == ".claude/devproc/" or p.rstrip("/") in lines]',
        'not ignored: the rules folder line not forced back (a team may commit the rules)'),
    'M48-split-joined-marker': ('        split_joined_markers_action(plan, current_exclude(target, m))\n',
        '',
        "migrated p30: the project's pattern works again"),
    'M49-records-of-gone-folders': ('                and os.path.isdir(d) and own_install(target, d)):',
        '                and own_install(target, d)):',
        'copy repair rollback: complete'),
    'M50-move-aside-modes': ('                os.rename(dist, aside)\n',
        '                make_writable(dist)\n                os.rename(dist, aside)\n',
        "copy repair rollback: directory and registry as before (the copy's old record included)"),
    'M51-nested-install-records': ('inside(d, target) and own_install(target, d):',
        'inside(d, target):',
        'nested: the inner 0.2.0 install keeps its record'),
    'M52-symlink-refusal': ('        if os.path.islink(os.path.join(target, rel)):\n            raise Fail("%s is a symlink',
        '        if False:\n            raise Fail("%s is a symlink',
        'symlinked .claude/devproc: install refused'),
    'M53-unnamed-main-checkout': ('        if g and g[1] is None:\n            raise Fail(',
        '        if False:\n            raise Fail(',
        'worktree of a bare repo: refused'),
    'M54-unsafe-layout-on-upgrade': ('        if args[0] in ("install", "upgrade", "repair"):\n            refuse_unsafe_layout(target, with_seeds=args[0] != "repair")',
        '        if args[0] == "install":\n            refuse_unsafe_layout(target, with_seeds=args[0] != "repair")',
        'settings.local.json tracked after install: repair and upgrade refused'),
    'M55-killed-run-leftovers': ('    if left:\n        raise Fail("%s has devproc leftovers',
        '    if False:\n        raise Fail("%s has devproc leftovers',
        'killed run leftovers: install refused'),
    'M56-nohup-respected': ('        if signal.getsignal(s) is not signal.SIG_IGN:  # nohup',
        '        if True:  # nohup',
        'nohup: run completes despite SIGHUP'),
    'M57-sighup-rollback': ('            signal.signal(s, interrupted)',
        '            pass',
        'SIGHUP: exit 1 (rolled back)'),
    'M58-broken-gitfile': ('        if "not a git repository (or any" in r.stderr.lower():',
        '        if "not a git repository" in r.stderr.lower():',
        'broken .git file: status exit 4, names the git error'),
    'M59-adopt-marked-lines': ('("/" if p.endswith("/") else "") in mine]',
        '("/" if p.endswith("/") else "") in set()]',
        'reinstall: after the last uninstall the exclude file is as before the first install'),
    'M60-signal-hold': ('    if SIGNALS["held"]:\n        return',
        '    if False:\n        return',
        'SIGHUP twice: exit 1 (rolled back)'),
    'M61-sigint-handled': ('    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):',
        '    for s in (signal.SIGTERM, signal.SIGHUP):',
        'SIGINT twice: exit 1 (rolled back)'),
    'M62-child-finishes': ('            try:\n                p.communicate(timeout=CLI_TIMEOUT)\n            except subprocess.TimeoutExpired:\n                pass',
        '            try:\n                stop(p)\n            except subprocess.TimeoutExpired:\n                pass',
        'SIGHUP: the running claude call was let finish'),
    'M63-own-session': ('stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)',
        'stderr=subprocess.PIPE, text=True, env=env, start_new_session=False)',
        'SIGHUP: the running claude call was let finish'),
    'M64-git-c-locale': ('        env.update(LC_ALL="C", LANGUAGE="")',
        '        pass',
        'git translated: a non-git directory installs (exit 0)'),
    'M65-git-1x-case': ('in r.stderr.lower():  # git 1.x',
        'in r.stderr:  # git 1.x',
        'git old: a non-git directory installs (exit 0)'),
    'M66-git-repo-vars': ('    env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_VARS}',
        '    env = dict(os.environ)',
        'GIT_DIR from the caller: the other repository untouched'),
    'M67-quote-leftover': ('"chmod -R u+w %s && rm -rf %s" % (q, q)',
        '"chmod -R u+w \'%s\' && rm -rf \'%s\'" % (p, p)',
        'killed run leftovers: the remove command is printed'),
    'M68-install-split-marker': ('        split_joined_markers_action(plan, {"file": ex_file})',
        '        pass',
        "joined marker: the project's pattern works while installed"),
    'M69-uninstall-symlink': ('        elif os.path.lexists(os.path.join(target, MANIFEST)):',
        '        elif False:',
        'symlinked .claude: uninstall refused (exit 1)'),
    'M70-say-gone-terminal': ('    except OSError:\n        quiet()',
        '    except ZeroDivisionError:\n        quiet()',
        'pipe closed: install exit 0'),
    'M71-quiet': ('            os.dup2(devnull, fd)',
        '            pass',
        'pipe closed: install exit 0'),
    'M72-rollback-held': ('            SIGNALS["held"] = True  # no interrupt may cut',
        '            pass  # no interrupt may cut',
        'failed step, then SIGHUP during the undo: exit 1 (rolled back)'),
    'M73-kill-group': ('        os.killpg(p.pid, signal.SIGKILL)',
        '        p.kill()',
        "timeout: the hung call's own child processes are killed"),
    'M74-reap-bounded': ('        p.communicate(timeout=5)',
        '        p.communicate()',
        'timeout: done within 200 s, though an escaped grandchild still holds the output'),
    'M75-tidy-no-rollback': ('            if tidying:\n                left = ',
        '            if False:\n                left = ',
        'interrupted while tidying: the upgrade is kept (exit 0, loads 9.9.49)'),
    'M76-timeout-bounds': ('t.isascii() and t.isdigit() and 1 <= int(t) <= 86400',
        't.isdigit() and int(t) >= 1',
        'DEVPROC_CLI_TIMEOUT=86401: exit 2, no traceback'),
    'M77-tidy-not-done-listed': ('                left = [d for d, _, _, w in self.steps[at - 1:] if w]',
        '                left = []',
        'interrupted while tidying: names the step not done'),
    'M78-tidy-done-not-listed': ('                left = [d for d, _, _, w in self.steps[at - 1:] if w]',
        '                left = [d for d, _, _, w in self.steps[tidying - 1:] if w]',
        'interrupted while tidying: a tidying step already done is not listed'),
}


def run_e2e(src, env, log):
    with open(log, "w") as f:
        return subprocess.run([os.path.join(src, "tests", "installer-e2e.sh")], stdin=subprocess.DEVNULL,
                              stdout=f, stderr=subprocess.STDOUT, env=env).returncode


def first_bad(log):
    for line in open(log):
        if line.startswith("BAD"):
            return line.strip()
    return ""


def main(argv):
    jobs = 4
    if argv[:1] == ["-j"]:
        jobs, argv = int(argv[1]), argv[2:]
    names = [n for n in MUTANTS if not argv or n.split("-")[0] in argv or n in argv]
    work = tempfile.mkdtemp(prefix="devproc-mutants-")
    env = dict(os.environ, E2E_FAIL_FAST="1")
    if not env.get("OLD_SRC"):
        if subprocess.run(["git", "-C", SRC, "rev-parse", "-q", "--verify", "refs/tags/v0.2.0"],
                          capture_output=True).returncode == 0:
            old = os.path.join(work, "old020")
            os.makedirs(old)
            arc = subprocess.run(["git", "-C", SRC, "archive", "v0.2.0"], capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", old], input=arc, check=True)
            env["OLD_SRC"] = old
        else:
            print("OLD_SRC is not set and there is no v0.2.0 tag: the migration checks would fail every run")
            return 2
    ignore = shutil.ignore_patterns(".git", "__pycache__", "docs")
    lib = os.path.join("lib", "devproc_install.py")
    base = os.path.join(work, "base")
    shutil.copytree(SRC, base, ignore=ignore)
    for name in names:
        a, b, _ = MUTANTS[name]
        d = os.path.join(work, name)
        shutil.copytree(SRC, d, ignore=ignore)
        p = os.path.join(d, lib)
        text = open(p).read()
        if text.count(a) != 1:
            print("%s: its text occurs %d times in %s (must be once): update the mutant" % (name, text.count(a), lib))
            return 2
        try:  # a mutant that does not even parse would fail every run and prove nothing
            ast.parse(text.replace(a, b))
        except SyntaxError as e:
            print("%s: the mutated file does not parse (%s): update the mutant" % (name, e))
            return 2
        open(p, "w").write(text.replace(a, b))
    print("unmutated run ...", flush=True)
    base_env = dict(env)
    base_env.pop("E2E_FAIL_FAST")
    rc = run_e2e(base, base_env, os.path.join(work, "base.log"))
    if rc != 0:
        print("the unmutated run FAILED (%s); see %s" % (first_bad(os.path.join(work, "base.log")), work))
        return 1
    print("unmutated run passed; %d mutant(s), %d at a time" % (len(names), jobs), flush=True)
    survived = []
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        futs = {pool.submit(run_e2e, os.path.join(work, n), env, os.path.join(work, n + ".log")): n for n in names}
        for fut in concurrent.futures.as_completed(futs):
            n = futs[fut]
            bad = first_bad(os.path.join(work, n + ".log"))
            if fut.result() == 0:
                survived.append(n)
                print("SURVIVED %s" % n, flush=True)
            elif MUTANTS[n][2] not in bad:
                # some other check failed first (a flaky session, a machine problem): proves nothing
                survived.append(n)
                print("UNCLEAR  %s: expected check %r, got %s" % (n, MUTANTS[n][2], bad or "no BAD line"), flush=True)
            else:
                print("caught   %s: %s" % (n, bad), flush=True)
    print("%d/%d mutant(s) caught" % (len(names) - len(survived), len(names)))
    if survived:
        print("survived or unclear: %s (logs in %s)" % (" ".join(sorted(survived)), work))
        return 1
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
