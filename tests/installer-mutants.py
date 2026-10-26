#!/usr/bin/env python3
"""Mutation test of the installer: proves tests/installer-e2e.sh (and tests/installer-killsweep.sh,
for a mutant whose expected check starts with "sweep: ") catches each safeguard.

Each mutant removes one safeguard from lib/devproc_install.py in a throwaway copy of this
source; the test run on that copy must fail. A mutant whose run passes ("SURVIVED")
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
    'M01-same-version-refusal': ('    if layout >= LAYOUT and base["version"] == VERSION and phash != base["pluginHash"]:',
        '    if False:',
        'same version, changed source: upgrade should refuse'),
    'M02-snapshot-hash': ('        return tree_hash(snap) == plugin_hash and read_json(mkf) == mk',
        '        return read_json(mkf) == mk',
        'damaged: status exit 4'),
    'M03-readonly': ('        fsync_dir(os.path.dirname(dst))\n        make_readonly(dst)',
        '        fsync_dir(os.path.dirname(dst))',
        'p12: snapshot is read-only'),
    'M04-prune': ('    if not os.path.isdir(base) or os.path.islink(base):\n        return',
        '    if True:\n        return',
        'plugin snapshots removed'),
    'M05-relocated-detect': ('    relocated = target != m["projectPath"]',
        '    relocated = False',
        'moved: gets a marketplace named for its new place'),
    'M06-uninstall-marketplace-remove': ('    if reg is not None and ours:',
        '    if False:',
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
    'M11-pending-previous': ('    return dict(m, **m["previous"]) if m.get("pending") and isinstance(m.get("previous"), dict) else m',
        '    return m',
        'upgrade rerun: the snapshot older than the previous one removed'),
    'M12-health-registry': ('    elif not same_path(reg, dist):',
        '    elif False:',
        'hijacked: status exit 4'),
    'M13-health-enabled': ('    if not enabled(target, m["pluginId"]):\n        out.append',
        '    if False:\n        out.append',
        'disabled: status exit 4'),
    'M14-exclude-own-file': ('    f = git_exclude_file(target)\n    out = dict(ex, file=f,',
        '    f = ex["file"]\n    out = dict(ex, file=f,',
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
    'M21-moved-aside-tidy': ('        if aside or moved_aside():',
        '        if aside:',
        'repair rerun: the snapshot moved aside is removed'),
    'M22-migrate-ownership': ('    if not by_us:',
        '    if False:',
        'not ours: migration should refuse'),
    'M23-lock': ('            lock(target)',
        '            pass',
        'locked: refused with exit 1'),
    'M24-repair-copy-record': ('        for d in local_records(target, old_pid):',
        '        for d in []:',
        "copy: its record of the original's plugin removed"),
    'M25-uninstall-copy-record': ('        for d in local_records(target, pid):',
        '        for d in []:',
        "copy 2, stopped repair, then uninstall: the copy's records removed"),
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
        'snapshot failure: no .partial copy, no snapshot under the real name'),
    'M30-manifest-first': ('        write_json(os.path.join(target, MANIFEST), dict(start["manifest"], pending=pending))',
        '        pass',
        'failed step: the manifest says the install is pending'),
    'M31-final-manifest-clears-pending': ('if k not in ("pending", "previous", "oldPluginInstalledByDevproc")}',
        'if k not in ("previous", "oldPluginInstalledByDevproc")}',
        'partial step, rerun: healthy, no longer pending'),
    'M32-exclude-newline': ('        add = b"\\n" if nl else b""',
        '        add = b""',
        "no final newline: the project's last pattern still works"),
    'M33-unchanged-was': ('    return h == rec["sha256"] or h == rec.get("was")',
        '    return h == rec["sha256"]',
        "upgrade killed before a rule update: the rule still counts as devproc's (as seeded)"),
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
    'M41-uninstall-ownership': ('    ours = reg is not None and (inside(reg, os.path.join(target, DIST)) or (',
        '    ours = reg is not None and (True or (',
        'copy 2: the original still healthy'),
    'M44-recorded-marketplace-json': ('    return mk if isinstance(mk, dict) else expected_marketplace(m["marketplace"])',
        '    return expected_marketplace(m["marketplace"])',
        'source edit: status still healthy'),
    'M39-settings-exclude-restore': ('    settings_will_stay = not m.get("settingsLocalExisted") and',
        '    settings_will_stay = True or',
        'failed step, then uninstall: directory and registry as before'),
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
        'copy: repair exit'),
    'M51-nested-install-records': ('inside(d, target) and own_install(target, d):',
        'inside(d, target):',
        'nested: the inner 0.2.0 install keeps its record'),
    'M52-symlink-refusal': ('        if os.path.islink(os.path.join(target, rel)):\n            raise Fail("%s is a symlink',
        '        if False:\n            raise Fail("%s is a symlink',
        'no manifest, symlinked .claude: uninstall refused (exit 1)'),
    'M53-unnamed-main-checkout': ('        if g and g[1] is None:\n            raise Fail(',
        '        if False:\n            raise Fail(',
        'worktree of a bare repo: refused'),
    'M54-unsafe-layout-on-upgrade': ('        if args[0] in ("install", "upgrade", "repair"):\n            refuse_unsafe_layout(target, with_seeds=args[0] != "repair")',
        '        if args[0] == "install":\n            refuse_unsafe_layout(target, with_seeds=args[0] != "repair")',
        'settings.local.json tracked after install: repair and upgrade refused'),
    'M55-killed-run-leftovers': ('    if left:  # devproc 0.2.2 and later',
        '    if False:  # devproc 0.2.2 and later',
        'killed run leftovers: install refused'),
    'M56-nohup-respected': ('        if signal.getsignal(s) is not signal.SIG_IGN:  # nohup',
        '        if True:  # nohup',
        'nohup: run completes despite SIGHUP'),
    'M57-sighup-handled': ('            signal.signal(s, interrupted)',
        '            pass',
        'SIGHUP: exit 3, stopped before the next step'),
    'M58-broken-gitfile': ('        if "not a git repository (or any" in r.stderr.lower():',
        '        if "not a git repository" in r.stderr.lower():',
        'broken .git file: status exit 4, names the git error'),
    'M59-adopt-marked-lines': (' or rel(p) in mine or rel(p) in theirs]',
        ' or rel(p) in theirs]',
        'reinstall: after the last uninstall the exclude file is as before the first install'),
    'M60-first-interrupt-waits': ('    if INTERRUPTS["n"] > 1:',
        '    if INTERRUPTS["n"] > 0:',
        'SIGHUP: exit 3, stopped before the next step'),
    'M61-sigint-handled': ('    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):',
        '    for s in (signal.SIGTERM, signal.SIGHUP):',
        'SIGINT twice: the first interrupt is announced (stops after the current step)'),
    'M62-child-left-running': ('            p.wait(timeout=CLI_TIMEOUT)\n        except subprocess.TimeoutExpired:',
        '            p.wait(timeout=CLI_TIMEOUT)\n        except KeyboardInterrupt:\n            stop(p)\n            raise\n        except subprocess.TimeoutExpired:',
        'SIGHUP twice: a rerun while the claude call it left still runs is refused (that call holds the lock)'),
    'M63-own-session': ('start_new_session=True, pass_fds=',
        'start_new_session=False, pass_fds=',
        'SIGHUP twice: says it stopped at once'),
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
    'M72-stop-before-step': ('            if INTERRUPTS["n"] and not warn:',
        '            if False:',
        'SIGHUP: exit 3, stopped before the next step'),
    'M73-kill-group': ('        os.killpg(p.pid, signal.SIGKILL)',
        '        p.kill()',
        "timeout: the hung call's own child processes are killed"),
    'M76-timeout-bounds': ('t.isascii() and t.isdigit() and 1 <= int(t) <= 86400',
        't.isdigit() and int(t) >= 1',
        'DEVPROC_CLI_TIMEOUT=86401: exit 2, no traceback'),
    'M79-atomic-write': ('        failpoint("write:" + os.path.basename(path))\n        os.replace(tmp, path)',
        '        os.remove(tmp)\n        with open(path, "wb") as g:\n            failpoint("write:" + os.path.basename(path))\n            g.write(data)',
        'killed in the first step: killed (exit 137), a temp file and no manifest'),
    'M80-copy-pending-seed': ('            if "was" in rec and rec["was"] is None:',
        '            if False:',
        'killed between copies: a rerun copies the rest'),
    'M81-registry-lock': ('            lock_config()',
        '            pass',
        'interrupt while waiting for the lock: exit 3, says nothing was changed'),
    'M82-temp-cleanup': ('    if tmp:\n        plan.add("remove %d temp file(s)',
        '    if False:\n        plan.add("remove %d temp file(s)',
        'p60 uninstall exit'),
    'M83-unfinished-uninstall-refusal': ('    if m.get("pending") == "uninstall":\n        raise unfinished_uninstall(target)',
        '    if False:\n        raise unfinished_uninstall(target)',
        'killed during uninstall: install refuses until it is finished'),
    'M84-lock-wait-interrupt': ('        if INTERRUPTS["n"]:\n            os.close(fd)',
        '        if False:\n            os.close(fd)',
        'interrupt while waiting for the lock: exit 3, says nothing was changed'),
    'M85-uninstall-own-folders': ('        for d in [DIST, ".claude/devproc", ".claude/rules/devproc"] + list(reversed(m.get("createdDirs", []))):',
        '        for d in list(reversed(m.get("createdDirs", []))):',
        "killed at the first manifest write: after uninstall, devproc's own folders are gone"),
    'M86-pending-install-ignores': ('    if m.get("pending") == "install":  # finishing an install',
        '    if False:  # finishing an install',
        'killed between copies: after the rerun, git status clean (all ignored)'),
    'M87-status-pending': ('    if m.get("pending"):\n        q = shlex.quote(target)',
        '    if False:\n        q = shlex.quote(target)',
        'failed step: status reports the unfinished run (exit 4)'),
    'M88-uninstall-marks-pending': ('             lambda: write_json(mpath, dict(m, pending="uninstall")))',
        '             lambda: None)',
        'killed during uninstall: status says the uninstall did not finish (exit 4)'),
    'M89-uninstall-previous-identity': ('        if before is not m and before["marketplace"] != m["marketplace"]:',
        '        if False:',
        "copy 2, stopped repair, then uninstall: the copy's records removed"),
    'M90-uninstall-stopped-migration': ('        if "oldPluginInstalledByDevproc" in m:  # a stopped migration',
        '        if False:  # a stopped migration',
        'migration killed (uninstall), then uninstall: the 0.2.0 plugin neither enabled nor recorded here'),
    'M91-split-on-migration-rerun': ('        split_joined_markers_action(plan, current_exclude(target, base))  # a stopped',
        '        pass  # a stopped',
        "migration killed, rerun: the project's last pattern works again"),
    'M92-exclude-record-same-file': ('            or (os.path.realpath(ex["file"]) != os.path.realpath(f)\n',
        '            or (True\n',
        'moved, repair killed, rerun, uninstall: exclude file byte-identical'),
    'M93-stale-pending-name': ('        if m.get("pending") and m["marketplace"] not in (base["marketplace"], marketplace_name(target)):',
        '        if False:',
        "moved twice, rerun: the middle place's marketplace removed"),
    'M94-uninstall-empty-own-dirs': ('        left = empty_own_dirs(target)\n',
        '        left = []\n',
        "uninstall killed after removing the manifest: a rerun removes devproc's empty folders"),
    'M95-locks-to-child': ('pass_fds=LOCKS if args[0] == "claude" else ()',
        'pass_fds=()',
        'SIGHUP twice: a rerun while the claude call it left still runs is refused (that call holds the lock)'),
    'M96-locks-held-by-wrapper': ('            argv = [sys.executable, "-I", "-c", HOLD] + args if args[0] == "claude" else args',
        '            argv = args',
        'a daemon a claude call started: the next run is not locked out'),
    'M97-exclude-not-utf8': ('    atomic_write(ex["file"], text.encode("utf-8", "surrogateescape"))',
        '    atomic_write(ex["file"], text.encode("utf-8"))',
        'p62 uninstall exit'),
    'M98-settings-previous-keys': ('    for x in (m, previous(m)):',
        '    for x in (m,):',
        'moved, repair killed, then uninstall: the exclude file as before install'),
    'M99-no-manifest-temp-cleanup': ('        plan = Plan(apply, uninstall=True)\n        temp_cleanup_action(plan, target)\n        if left:',
        '        plan = Plan(apply, uninstall=True)\n        if left:',
        'p60 uninstall exit'),
    'M100-leave-old-place-is-work': ('            plan.add(desc, lambda d=d: run(["claude", "plugin", "uninstall", old_pid, "--scope", "local", "-y"], cwd=d))',
        '            plan.add(desc, lambda d=d: run(["claude", "plugin", "uninstall", old_pid, "--scope", "local", "-y"], cwd=d), warn=True)',
        'copy 2 repair stopped: exit 3'),
    'M101-worktree-shared-exclude': ('        shared = [l for l in ex["lines"] if l in theirs and l not in keep_lines]',
        '        shared = []',
        "worktree copy uninstall: the main checkout's exclude file untouched"),
    'M102-json-utf8': ('    with open(path, encoding="utf-8") as f:  # JSON is UTF-8',
        '    with open(path) as f:  # JSON is UTF-8',
        'Latin-1 locale: settings.local.json byte-identical after install and uninstall'),
    'M103-no-manifest-symlink': ('        refuse_symlinks(target, with_seeds=False)  # leftovers',
        '        pass  # leftovers',
        'no manifest, symlinked .claude: uninstall refused (exit 1)'),
    'M104-write-through-link': ('    path = os.path.realpath(path)\n',
        '',
        'symlinked exclude: still a symlink after install'),
    'M105-other-worktrees': ('        others |= {os.path.realpath',
        '        others |= set() and {os.path.realpath',
        "main checkout uninstall: the worktree copy's devproc files still ignored"),
    'M106-recorded-installs': ('               if str(p.get("id", "")).startswith("devproc@")',
        '               if False',
        "shared linked exclude file: after the second repo's uninstall, the first still healthy"),
    'M107-own-manifest-via-link': (' or same_path(os.path.join(d, MANIFEST), os.path.join(target, MANIFEST)):',
        ':',
        'worktree with a linked .claude: main checkout uninstall leaves the exclude file as before'),
    'M108-mark-kept': ('    out = [kept_mark(target) if l == MARK and',
        '    out = [kept_mark(target) if False and',
        'kept for files left behind: the marker above the kept line names this directory'),
    'M109-take-kept-back': ('            if mine[i] == kept_mark(target) and mine[i + 1] in rels:',
        '            if False:',
        'reinstall: its kept lines are devproc\'s own again (no kept marker left)'),
    'M110-pending-repair-symlinks': ('        refuse_symlinks(target)  # finishing a pending upgrade',
        '        pass  # finishing a pending upgrade',
        'pending upgrade, symlinked rule: repair refused (exit 1)'),
    'M111-adopted-settings-line': ('                if settings_will_stay or (m.get("settingsLocalExisted") and line in ex.get("adopted", [])):',
        '                if settings_will_stay:',
        'own settings entries: after the second uninstall the settings file still ignored'),
    'M112-shared-kept-pair': ('           and lines[i + 1] not in shared else l',
        '           else l',
        'kept line for a worktree copy deleted by hand: the main checkout still healthy'),
    'M113-remove-own-kept': ('        if (lines[i].endswith(MARK) or lines[i] == kept_mark(target)) and',
        '        if (lines[i].endswith(MARK)) and',
        'uninstall killed after marking, files removed, rerun: exclude file as before'),
}


SWEEP = "sweep: "  # an expected check with this prefix is one of tests/installer-killsweep.sh's


def script_for(name):
    return "installer-killsweep.sh" if MUTANTS[name][2].startswith(SWEEP) else "installer-e2e.sh"


def expected_check(name):
    c = MUTANTS[name][2]
    return c[len(SWEEP):] if c.startswith(SWEEP) else c


def run_e2e(src, env, log, script="installer-e2e.sh"):
    with open(log, "w") as f:
        return subprocess.run([os.path.join(src, "tests", script)], stdin=subprocess.DEVNULL,
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
    for script in sorted({script_for(n) for n in names} | {"installer-e2e.sh"}):
        log = os.path.join(work, "base-%s.log" % script)
        if run_e2e(base, base_env, log, script) != 0:
            print("the unmutated %s run FAILED (%s); see %s" % (script, first_bad(log), work))
            return 1
    print("unmutated run passed; %d mutant(s), %d at a time" % (len(names), jobs), flush=True)
    survived = []
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        futs = {pool.submit(run_e2e, os.path.join(work, n), env, os.path.join(work, n + ".log"), script_for(n)): n
                for n in names}
        for fut in concurrent.futures.as_completed(futs):
            n = futs[fut]
            bad = first_bad(os.path.join(work, n + ".log"))
            if fut.result() == 0:
                survived.append(n)
                print("SURVIVED %s" % n, flush=True)
            elif expected_check(n) not in bad:
                # some other check failed first (a flaky session, a machine problem): proves nothing
                survived.append(n)
                print("UNCLEAR  %s: expected check %r, got %s" % (n, expected_check(n), bad or "no BAD line"), flush=True)
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
