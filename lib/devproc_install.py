#!/usr/bin/env python3
"""devproc installer: install | upgrade | repair | uninstall | status, per target directory.

Dry run by default: prints every action. --apply performs them.
Safety rules:
  - writes only inside TARGET/.claude/, TARGET's local git exclude file (never
    committed), and Claude Code's own plugin registry via the `claude plugin` CLI;
  - each directory runs its own read-only snapshot of the plugin,
    TARGET/.claude/devproc/dist/<version>/, registered as a marketplace of its own
    (devproc-<id>): directories never share plugin code or registry entries, and
    edits to the devproc source never reach an installed directory;
  - a version's code never changes: a source that differs from what a directory has
    under the same version is refused (raise the version); repair restores a damaged
    snapshot only from a source with the recorded hash;
  - never replaces a file it did not write, or one changed since it wrote it; in
    settings.local.json and the exclude file it only adds and removes its own entries;
  - inside git, TARGET must be the top level of the main checkout (Claude Code keeps
    local plugin settings there);
  - the guard self-test must pass, and must fail on a mutant, before anything is written;
  - install, upgrade and repair roll back on a failed step; uninstall stops and can be rerun;
  - one run at a time per directory; running install twice changes nothing.
Exit codes: 0 done (status: healthy), 1 refused or failed and rolled back (status: not
installed), 2 usage, 3 rollback incomplete, uninstall stopped, or unexpected error,
4 status found problems.
stdlib only. devproc itself makes no network calls (the `claude` CLI is Claude Code's own).
"""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PLUGIN = os.path.join(SRC, "plugin")
TPL = os.path.join(PLUGIN, "templates")
VERSION = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))["version"]
LAYOUT = 2
MANIFEST = ".claude/devproc/manifest.json"
SETTINGS_LOCAL = ".claude/settings.local.json"
DIST = ".claude/devproc/dist"
EXCLUDE_PATHS = [".claude/devproc/", ".claude/rules/devproc/", SETTINGS_LOCAL]
DIRS = [".claude", ".claude/rules", ".claude/rules/devproc", ".claude/devproc", DIST]
MARK = "# devproc: added by devproc install.sh, removed by uninstall.sh"
MAX_LISTED = 20
CLI_TIMEOUT = 300  # DEVPROC_CLI_TIMEOUT overrides it
# git's variables that name a repository (git rev-parse --local-env-vars, less the config ones): set
# by a git hook or a vcsh/yadm shell, they would point git at that repository instead of the target
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_GRAFT_FILE",
                 "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_CONFIG")
# layout 1 (devproc 0.2.0): one marketplace "devproc" for every directory, run in place
# from the source clone. Kept only to upgrade and uninstall such installs.
OLD_MARKETPLACE = "devproc"
OLD_ID = "devproc@devproc"
LAYOUT2_KEYS = ("version", "pluginHash", "marketplace", "pluginId", "dist", "projectPath", "files")


def seeds():
    """(template path, path relative to target) for every seeded file."""
    out = [(os.path.join(TPL, "rules", f), ".claude/rules/devproc/" + f)
           for f in sorted(os.listdir(os.path.join(TPL, "rules"))) if f.endswith(".md")]
    out.append((os.path.join(TPL, "config.json"), ".claude/devproc/config.json"))
    out.append((os.path.join(TPL, "MISTAKES.md"), ".claude/devproc/MISTAKES.md"))
    return out


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_hash(root):
    """sha256 over the tree under root: each path with its kind (file, directory, symlink), a file's
    exec bit and bytes, a symlink's target. Skips Python caches. Write bits are not hashed (the
    snapshot is made read-only)."""
    h = hashlib.sha256()
    for d, subdirs, fs in os.walk(root):
        subdirs[:] = sorted(s for s in subdirs if s != "__pycache__")
        entries = [(n, True) for n in subdirs] + [(n, False) for n in fs if not n.endswith(".pyc")]
        for n, is_dir in sorted(entries):
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).encode()
            if os.path.islink(p):
                h.update(b"L\0" + rel + b"\0" + os.readlink(p).encode() + b"\0")
            elif is_dir:
                h.update(b"D\0" + rel + b"\0")
            else:
                h.update(b"F\0" + rel + b"\0" + (b"x" if os.stat(p).st_mode & 0o100 else b"-") + b"\0")
                with open(p, "rb") as fh:
                    h.update(fh.read())
                h.update(b"\0")
    return h.hexdigest()


def say(msg):
    """Print a line. A gone terminal (ssh dropped, `| head` closed) must not stop a rollback: on the
    first failed write (EIO, EPIPE), output goes to /dev/null and the run carries on."""
    try:
        print(msg, flush=True)
    except OSError:
        quiet()


def quiet():
    devnull = os.open(os.devnull, os.O_WRONLY)
    for fd in (1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


class Fail(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


# Ctrl-C, SIGTERM and SIGHUP (main installs interrupted() for each one not ignored, as nohup
# ignores SIGHUP). The first raises KeyboardInterrupt, which starts the rollback; from then on they
# are held: a second one (closing a terminal can send SIGHUP twice) would cut the rollback short.
# Inside steady() an interrupt waits until the block ends.
SIGNALS = {"held": False, "steady": 0, "pending": False}


def interrupted(signum, frame):
    if SIGNALS["held"]:
        return
    if SIGNALS["steady"]:
        SIGNALS["pending"] = True
        return
    SIGNALS["held"] = True
    raise KeyboardInterrupt


@contextlib.contextmanager
def steady():
    """A block an interrupt must not cut: a file rewrite (cut after the truncate, the file would be
    left empty) or a process start (cut before Popen returns, the child would run unwatched)."""
    SIGNALS["steady"] += 1
    try:
        yield
    finally:
        SIGNALS["steady"] -= 1
    if not SIGNALS["steady"] and SIGNALS["pending"]:
        SIGNALS["pending"] = False
        if not SIGNALS["held"]:  # held: a rollback has started, which no interrupt may cut
            SIGNALS["held"] = True
            raise KeyboardInterrupt


def stop(p):
    """Kill p and what it started (its own process group), and reap it: a grandchild still holding
    the output pipes must not keep devproc waiting."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.stdout.close()
        p.stderr.close()
        p.wait()


def run(args, cwd=None, check=True):
    env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_VARS}
    if args[0] == "git":
        env.update(LC_ALL="C", LANGUAGE="")  # its messages are matched below ("not a git repository")
    # A session of its own: a terminal's Ctrl-C or hangup reaches devproc only, and an interrupt lets
    # the call finish before devproc rolls back. Killing `claude` mid-call could leave the plugin
    # registry that every directory shares half written.
    p = None
    timed_out = False
    try:
        with steady():
            try:
                p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
            except OSError as e:
                raise Fail("`%s` could not run: %s" % (" ".join(args), e))
        out, err = p.communicate(timeout=CLI_TIMEOUT)
    except KeyboardInterrupt:
        if p is not None and p.poll() is None:
            say("  interrupted: letting `%s` finish first (up to %d s); further interrupts are ignored"
                % (" ".join(args[:4]), CLI_TIMEOUT))
            try:
                p.communicate(timeout=CLI_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        raise
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if p is not None and p.poll() is None:  # timed out, or an interrupt landed before the wait
            with steady():
                stop(p)
    if timed_out:
        raise Fail("`%s` did not finish within %d s" % (" ".join(args), CLI_TIMEOUT))
    r = subprocess.CompletedProcess(args, p.returncode, out, err)
    if check and r.returncode != 0:
        raise Fail("`%s` exit %d: %s" % (" ".join(args), r.returncode,
                                        (r.stderr or r.stdout).strip()[:400]))
    return r


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with steady(), open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------------ plan / apply

class Plan:
    """Ordered steps. rollback=True: a failed step is undone (its own partial work, then every
    completed step, in reverse). rollback=False (uninstall): a failed step stops the run;
    rerunning finishes it. A step added with warn=True is tidying after the work is done: its
    failure is reported and never rolled back."""

    def __init__(self, apply, rollback=True):
        self.apply = apply
        self.rollback = rollback
        self.steps = []
        self.notes = []

    def add(self, desc, do, undo=None, warn=False):
        if self.rollback and not warn and any(s[3] for s in self.steps):
            raise ValueError("a step after a tidying step could not be rolled back: %s" % desc)
        self.steps.append((desc, do, undo, warn))

    def note(self, msg):
        self.notes.append(msg)

    def execute(self):
        tag = "" if self.apply else "[dry-run] "
        for n in self.notes:
            say("  note: " + n)
        if not self.steps:
            say("  nothing to do")
            return
        done = []
        current = None  # the step running now: its own partial work is undone first
        tidying = 0  # the first tidying step's number, once reached: tidying may have removed what an undo needs
        at = 0
        try:
            for i, (desc, do, undo, warn) in enumerate(self.steps, 1):
                current = None
                at = i
                if warn and self.rollback and self.apply and not tidying:
                    tidying = i
                say("  %s[%d/%d] %s" % (tag, i, len(self.steps), desc))
                if not self.apply:
                    continue
                if warn:
                    try:
                        do()
                    except Exception as e:
                        say("  warning: this step failed (%s). The work before it is done and kept; "
                            "do this step by hand." % e)
                    at = i + 1  # done (or reported): not listed as not done
                    continue
                current = (desc + " (partial)", undo)
                do()
                done.append((desc, undo))
                current = None
        except (Exception, KeyboardInterrupt) as e:
            SIGNALS["held"] = True  # no interrupt may cut the rollback (or the report of a stop) short
            if tidying:
                left = [d for d, _, _, w in self.steps[at - 1:] if w]
                say(("  interrupted while tidying up: the work is done and kept" if isinstance(e, KeyboardInterrupt)
                     else "  warning: tidying stopped (%s). The work is done and kept" % e)
                    + ("; these tidying steps were not done, or not confirmed done: check each and do it by hand"
                       " if needed:" if left else "."))
                for d in left:
                    say("    not done: " + d)
                return
            say("  FAILED: %s" % (e if not isinstance(e, KeyboardInterrupt) else "interrupted"))
            if not self.rollback:
                raise Fail("stopped at the failed step; %d step(s) were done and are not undone. "
                           "Fix the cause and rerun the same command: it is safe to rerun." % len(done), 3)
            say("  rolling back %d step(s)" % len(done))
            failed = 0
            for d, u in ([current] if current else []) + list(reversed(done)):
                if not u:
                    continue
                try:
                    u()
                    say("    undone: " + d)
                except (Exception, KeyboardInterrupt) as e2:
                    failed += 1
                    say("    UNDO FAILED (%s): %s" % (d, e2 if not isinstance(e2, KeyboardInterrupt) else "interrupted"))
            if failed:
                raise Fail("rollback INCOMPLETE: %d undo step(s) failed, see UNDO FAILED above" % failed, 3)
            raise Fail("rolled back; nothing from this run is left")
        if not self.apply:
            say("dry run only. Rerun with --apply to do it.")


# ------------------------------------------------------------------ checks

def git_top(target):
    """(top level, main checkout top level) if target is in a git work tree, else None. The main
    checkout is None for a worktree whose main checkout git cannot name (bare or --separate-git-dir). Any git error other than "not a git
    repository" (for example dubious ownership) stops the run: treating it as no repo would drop
    the exclude lines and the top-level check."""
    r = run(["git", "-C", target, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"], check=False)
    if r.returncode != 0:
        if "not a git repository (or any" in r.stderr.lower():  # git 1.x: "Not a git ..."
            return None  # a broken .git file says "not a git repository: <path>": that is an error
        raise Fail("git cannot read the repository at %s: %s" % (target, r.stderr.strip()[:300]))
    top, gd, cd = r.stdout.split("\n")[:3]
    gd = os.path.realpath(os.path.join(target, gd))
    cd = os.path.realpath(os.path.join(target, cd))
    if gd == cd:
        return os.path.realpath(top), os.path.realpath(top)
    # a linked worktree: the main checkout is the first entry of `git worktree list`
    wl = run(["git", "-C", target, "worktree", "list", "--porcelain"]).stdout.split("\n")
    main = wl[0][len("worktree "):] if wl and wl[0].startswith("worktree ") else None
    if (main is None or "bare" in wl[1:wl.index("") if "" in wl else len(wl)]
            or not os.path.exists(os.path.join(main, ".git"))):
        # a bare repository, or one made with --separate-git-dir, where git lists the git dir itself
        return os.path.realpath(top), None
    return os.path.realpath(top), os.path.realpath(main)


def resolve_target(path, top_only=False):
    t = os.path.realpath(path)
    if not os.path.isdir(t):
        raise Fail("target is not a directory: %s" % t)
    home = os.path.realpath(os.path.expanduser("~"))
    if t in ("/", home):
        raise Fail("refusing %s: devproc is per project; pick a project directory" % t)
    if t == SRC or t.startswith(SRC + os.sep):
        raise Fail("refusing to install into the devproc source itself")
    if not os.access(t, os.W_OK):
        raise Fail("target not writable: %s" % t)
    if top_only:
        g = git_top(t)
        if g and g[1] is None:
            raise Fail("%s is a linked worktree whose main checkout git cannot name (a bare or --separate-git-dir "
                       "repository), so there is no place for Claude Code's local plugin settings: devproc "
                       "cannot be installed here" % t)
        if g and t != g[1]:
            why = "a linked git worktree" if g[0] != g[1] else "a subdirectory of a git repo"
            raise Fail("%s is %s. Claude Code keeps local plugin settings at the top of the main "
                       "checkout, so install there instead: %s. (An install already here can only be "
                       "removed: uninstall.sh --apply %s)" % (t, why, g[1], t))
    return t


def preflight(target):
    if sys.version_info < (3, 8):
        raise Fail("python3 >= 3.8 required")
    if not re.match(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$", VERSION):
        raise Fail("plugin.json version %r cannot name a directory" % VERSION)
    guard = os.path.join(PLUGIN, "scripts", "guard.py")
    st = os.path.join(SRC, "tests", "selftest.py")
    r = run([sys.executable, "-I", st, guard], check=False)
    last = (r.stdout.strip().splitlines() or ["no output"])[-1]
    if r.returncode != 0:
        raise Fail("guard self-test failed, exit %d (%s):\n%s%s" % (r.returncode, last, r.stdout, r.stderr))
    say("  ok: guard self-test: %s" % last)
    r = run([sys.executable, "-I", st, os.path.join(SRC, "tests", "mutant_noop.py")], check=False)
    if r.returncode != 1:
        raise Fail("self-test did not fail on the mutant guard (exit %d): the test proves nothing" % r.returncode)
    say("  ok: self-test fails on a mutant guard, as it must")
    run(["claude", "plugin", "validate", PLUGIN])
    run(["claude", "plugin", "validate", SRC])
    say("  ok: claude plugin validate (plugin, marketplace)")
    sl = os.path.join(target, SETTINGS_LOCAL)
    if os.path.exists(sl):
        try:
            d = read_json(sl)
        except ValueError as e:
            raise Fail("%s is not valid JSON (%s); fix it first" % (sl, e))
        if not isinstance(d, dict) or not all(isinstance(d.get(k, {}), dict)
                                              for k in ("enabledPlugins", "extraKnownMarketplaces")):
            raise Fail("%s is not a JSON object with object-valued enabledPlugins and "
                       "extraKnownMarketplaces; fix it first" % sl)


def refuse_symlinks(target, with_seeds=True, code=1):
    """Refuse a symlink at a devproc folder, the manifest, settings.local.json or (install, upgrade)
    a seeded file: devproc would follow it out of the target and read, write or remove another
    directory's install."""
    for rel in DIRS + ([rel for _, rel in seeds()] if with_seeds else []) + [MANIFEST, SETTINGS_LOCAL]:
        if os.path.islink(os.path.join(target, rel)):
            raise Fail("%s is a symlink; devproc would follow it out of %s and act on what it points to. "
                       "Replace it with a real folder or file, then rerun." % (os.path.join(target, rel), target),
                       code)


def refuse_unsafe_layout(target, with_seeds=True):
    """Refuse a target where devproc's writes could land elsewhere or be committed: a symlink (see
    refuse_symlinks), or a tracked settings.local.json (devproc writes absolute local paths into it)."""
    refuse_symlinks(target, with_seeds)
    if git_top(target) and tracked(target, SETTINGS_LOCAL):
        raise Fail("%s is tracked by git. devproc writes this directory's absolute paths into it, which "
                   "would then be committed. Untrack it first (git -C %s rm --cached %s), then rerun."
                   % (SETTINGS_LOCAL, target, SETTINGS_LOCAL))


def leftovers(target):
    """What a killed earlier run (no manifest written) left behind, with the command removing each."""
    out = []
    base = os.path.join(target, DIST)
    if os.path.isdir(base) and not os.path.islink(base):
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            q = shlex.quote(p)
            out.append(("plugin snapshot %s" % p, "chmod -R u+w %s && rm -rf %s" % (q, q)))
    return out


def marked_lines(ex_file):
    """Lines under a devproc marker in ex_file: devproc's own, from an earlier install whose
    uninstall kept them (files stayed behind) or a killed run."""
    if not os.path.exists(ex_file):
        return set()
    l = open(ex_file, newline="").read().split("\n")
    return {l[i + 1] for i in range(len(l) - 1) if l[i].endswith(MARK)}


def load_manifest(target):
    p = os.path.join(target, MANIFEST)
    if not os.path.exists(p):
        return None
    bad = "%s %%s. Restore it, or remove it and reinstall." % p
    try:
        m = read_json(p)
    except ValueError as e:
        raise Fail(bad % ("is not valid JSON (%s)" % e))
    if not isinstance(m, dict):
        raise Fail(bad % "is not a JSON object")
    layout = m.get("layout", 1)
    if not isinstance(layout, int) or isinstance(layout, bool):
        raise Fail(bad % "has a layout that is not a number")
    need = LAYOUT2_KEYS if layout >= LAYOUT else ("version", "files", "installedAt")
    missing = [k for k in need if k not in m]
    if missing:
        raise Fail(bad % ("lacks " + ", ".join(missing)))
    wrong = [k for k in need if not isinstance(m[k], dict if k == "files" else str)]
    if not wrong and not all(isinstance(v, dict) and isinstance(v.get("sha256"), str)
                             and v.get("origin") in ("seeded", "adopted") for v in m["files"].values()):
        wrong = ["files"]
    ex = m.get("exclude")
    if ex is not None and not (isinstance(ex, dict) and isinstance(ex.get("lines"), list)
                               and all(isinstance(x, str) for x in ex["lines"])):
        wrong.append("exclude")
    if not isinstance(m.get("createdDirs", []), list):
        wrong.append("createdDirs")
    if wrong:
        raise Fail(bad % ("has a wrong type for " + ", ".join(wrong)))
    return m


def local_settings(target):
    sl = os.path.join(target, SETTINGS_LOCAL)
    try:
        d = read_json(sl) if os.path.exists(sl) else {}
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    for k in ("enabledPlugins", "extraKnownMarketplaces"):
        if not isinstance(d.get(k, {}), dict):
            d[k] = {}
    return d


def enabled(target, pid):
    return bool(local_settings(target).get("enabledPlugins", {}).get(pid))


def plugin_list(target):
    try:
        return json.loads(run(["claude", "plugin", "list", "--json"], cwd=target).stdout)
    except ValueError as e:
        raise Fail("`claude plugin list --json` printed something that is not JSON (%s)" % e)


def list_entry(target, pid):
    """This directory's local-scope record of plugin pid in `claude plugin list --json`, or None."""
    for p in plugin_list(target):
        if (p.get("id") == pid and p.get("scope") == "local"
                and os.path.realpath(p.get("projectPath", "")) == target):
            return p
    return None


def registered_path(target, name):
    """Path the user-level registry holds for marketplace `name`, or None."""
    try:
        mks = json.loads(run(["claude", "plugin", "marketplace", "list", "--json"], cwd=target).stdout)
    except ValueError as e:
        raise Fail("`claude plugin marketplace list --json` printed something that is not JSON (%s)" % e)
    for mk in mks:
        if mk.get("name") == name:
            return mk.get("path") or mk.get("installLocation") or ""
    return None


def same_path(a, b):
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def inside(path, root):
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def marketplace_name(target):
    return "devproc-" + hashlib.sha256(target.encode()).hexdigest()[:8]


def plugin_id(name):
    return "devproc@" + name


def dist_dir(target, version):
    return os.path.join(target, DIST, version)


def expected_marketplace(name):
    mk = read_json(os.path.join(SRC, ".claude-plugin", "marketplace.json"))
    mk["name"] = name
    return mk


def snapshot_ok(dist, plugin_hash, mk):
    """True if dist holds an intact snapshot: plugin/ with this tree hash, and marketplace.json equal to mk."""
    snap = os.path.join(dist, "plugin")
    mkf = os.path.join(dist, ".claude-plugin", "marketplace.json")
    if not os.path.isdir(snap) or os.path.islink(snap) or not os.path.isfile(mkf) or os.path.islink(mkf):
        return False
    try:
        return tree_hash(snap) == plugin_hash and read_json(mkf) == mk
    except (OSError, ValueError):
        return False


def installed_marketplace(m):
    """The marketplace.json devproc wrote into this install's snapshot (recorded since 0.2.1; the
    source's own, renamed, for a manifest without the record)."""
    mk = m.get("marketplaceJson")
    return mk if isinstance(mk, dict) else expected_marketplace(m["marketplace"])


def remove_marketplace(target, name):
    """Remove marketplace `name` (its plugin records go with it). The local-scope remove is refused
    when the name is not declared in this directory's settings.local.json; then remove it from
    every scope, which is still only this name."""
    r = run(["claude", "plugin", "marketplace", "remove", name, "--scope", "local"], cwd=target, check=False)
    if r.returncode != 0:
        if "not declared in local settings" not in (r.stderr + r.stdout):
            raise Fail("`claude plugin marketplace remove %s --scope local` exit %d: %s"
                       % (name, r.returncode, (r.stderr or r.stdout).strip()[:400]))
        run(["claude", "plugin", "marketplace", "remove", name], cwd=target)


def local_records(target, pid):
    """Directories at or below target holding a local-scope record of plugin pid (a session started
    in a subdirectory adds a record of its own). A record whose folder was deleted is skipped: the
    CLI can only reach a record from its own folder."""
    out = []
    for p in plugin_list(target):
        d = os.path.realpath(p.get("projectPath", ""))
        if (p.get("id") == pid and p.get("scope") == "local" and inside(d, target) and d not in out
                and os.path.isdir(d) and own_install(target, d)):
            out.append(d)
    return out


def own_install(target, d):
    """True if directory d (at or below target) belongs to target's install, not to another devproc
    install nested inside it (a folder between them with a manifest of its own)."""
    d = os.path.realpath(d)
    while inside(d, target) and d != target:
        if os.path.exists(os.path.join(d, MANIFEST)):
            return False
        d = os.path.dirname(d)
    return True


def old_records(target):
    """Layout 1: (records of devproc@devproc at this directory or below it, records elsewhere or
    in another scope). Sessions started in a subdirectory add records of their own; a nested
    install's records belong to it."""
    mine, others = [], []
    for p in plugin_list(target):
        if p.get("id") != OLD_ID:
            continue
        d = p.get("projectPath", "")
        if p.get("scope") == "local" and inside(d, target) and own_install(target, d):
            mine.append(p)
        else:
            others.append(p)
    return mine, others


def git_exclude_file(target):
    # --git-path is relative to the -C dir unless absolute (no --path-format: that needs git 2.31+)
    ex = run(["git", "-C", target, "rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    return os.path.normpath(os.path.join(target, ex))


def ignored(target, rel):
    probe = rel + "probe" if rel.endswith("/") else rel
    return run(["git", "-C", target, "check-ignore", "-q", "--", probe], check=False).returncode == 0


def tracked(target, rel):
    """True if git tracks the file rel (then an exclude line cannot hide it)."""
    return run(["git", "-C", target, "ls-files", "--error-unmatch", "--", rel], check=False).returncode == 0


def required_ignores(m):
    """devproc paths git must ignore: .claude/devproc/ always (manifest, snapshot, task evidence),
    and settings.local.json if devproc added its exclude line (it holds absolute local paths).
    The rules folder is not required: a team may commit it (docs/02-how-it-works.md)."""
    lines = [l.lstrip("/").rstrip("/") for l in (m.get("exclude") or {}).get("lines", [])]
    return [p for p in (".claude/devproc/", SETTINGS_LOCAL) if p == ".claude/devproc/" or p in lines]


def current_exclude(target, m):
    """The manifest's exclude record, made right for where this directory is now: this directory's
    own exclude file (a moved or copied project carries its .git with it, so the recorded file can
    be the original's), and each line relative to the current top level (the target was the top
    at install). None outside git."""
    ex = m.get("exclude")
    g = git_top(target)
    if not ex or not g:
        return None
    lines = ["/" + os.path.relpath(os.path.join(target, l.lstrip("/").rstrip("/")), g[0]) + ("/" if l.endswith("/") else "")
             for l in ex["lines"]]
    f = git_exclude_file(target)
    out = dict(ex, file=f, lines=lines)
    if (not isinstance(ex.get("file"), str)
            or os.path.relpath(f, target) != os.path.relpath(ex["file"], m.get("projectPath", target))):
        # another exclude file now (the folder sits in another repo, or was moved without its .git):
        # the record of what devproc created or added in the old one says nothing about this one
        out.update(newlineBefore=None, fileCreated=False, dirCreated=False)
    return out


def health(target, m):
    """Problems with a layout-2 install, as (problem, fix) pairs. Empty = healthy."""
    out = []
    dist = os.path.join(target, m["dist"])
    if target != m["projectPath"]:
        out.append(("this directory was moved or copied from %s" % m["projectPath"], "--repair"))
    if not os.path.isdir(os.path.join(dist, "plugin")):
        out.append(("the plugin snapshot %s is missing" % os.path.join(dist, "plugin"), "--repair"))
    elif not snapshot_ok(dist, m["pluginHash"], installed_marketplace(m)):
        out.append(("the plugin snapshot %s changed since install" % dist, "--repair"))
    reg = registered_path(target, m["marketplace"])
    if reg is None:
        out.append(("marketplace %s is not registered with Claude Code" % m["marketplace"], "--repair"))
    elif not same_path(reg, dist):
        out.append(("marketplace %s points at %s, not this directory's snapshot" % (m["marketplace"], reg),
                    "--repair"))
    if not enabled(target, m["pluginId"]):
        out.append(("%s is not enabled in %s" % (m["pluginId"], SETTINGS_LOCAL), "--repair"))
    e = list_entry(target, m["pluginId"])
    if e is None:
        out.append(("Claude Code has no install record of %s for this directory" % m["pluginId"], "--repair"))
    elif e.get("errors"):
        out.append(("Claude Code reports load errors for %s: %s" % (m["pluginId"], "; ".join(map(str, e["errors"]))),
                    "--repair"))
    if git_top(target):
        for p in required_ignores(m):
            if p == SETTINGS_LOCAL and tracked(target, p):
                out.append(("%s is tracked by git, so devproc's local paths in it would be committed" % p,
                            "untrack it: git -C %s rm --cached %s" % (target, p)))
            elif not ignored(target, p):
                out.append(("%s is not ignored by git (devproc files there could be committed)" % p, "--repair"))
    return out


# ------------------------------------------------------------------ actions

def make_writable(path):
    """Add the owner's write bit under path. Never follows a symlink."""
    if not os.path.lexists(path) or os.path.islink(path):
        return
    os.chmod(path, os.stat(path).st_mode | 0o200)
    for d, subdirs, fs in os.walk(path):
        for n in subdirs + fs:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode | 0o200)


def make_readonly(path):
    for d, subdirs, fs in os.walk(path, topdown=False):
        for n in fs + subdirs:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode & ~0o222)
    os.chmod(path, os.stat(path).st_mode & ~0o222)


def remove_tree(path):
    """Remove a snapshot directory, or a stray file or symlink, never following a link."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
        return
    make_writable(path)
    shutil.rmtree(path)


def build_snapshot(dst, name):
    """Copy plugin/ to dst/plugin, write dst/.claude-plugin/marketplace.json named `name`,
    check the copy against the source, validate it, and make it read-only. Leaves nothing
    behind when it fails."""
    if os.path.lexists(dst):
        raise Fail("%s already exists" % dst)
    tmp = dst + ".partial"
    renamed = False
    try:
        remove_tree(tmp)
        os.makedirs(os.path.join(tmp, ".claude-plugin"))
        shutil.copytree(PLUGIN, os.path.join(tmp, "plugin"), symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        write_json(os.path.join(tmp, ".claude-plugin", "marketplace.json"), expected_marketplace(name))
        if tree_hash(os.path.join(tmp, "plugin")) != tree_hash(PLUGIN):
            raise Fail("the snapshot copy does not match the source")
        run(["claude", "plugin", "validate", tmp])
        os.rename(tmp, dst)  # rename first: Linux may refuse to move a directory that is not writable
        renamed = True
        make_readonly(dst)
    except BaseException:
        remove_tree(tmp)
        if renamed:
            remove_tree(dst)
        raise


def snapshot_action(plan, dst, name):
    state = {}

    def do():
        if os.path.lexists(dst):
            raise Fail("%s already exists" % dst)
        state["ours"] = True
        build_snapshot(dst, name)

    def undo():  # never removes a snapshot this step did not write
        if state.get("ours"):
            remove_tree(dst)
    plan.add("write read-only plugin snapshot %s (marketplace %s)" % (dst, name), do, undo)


def settings_backup(target):
    """(do, undo) pair: do saves settings.local.json, undo restores those exact bytes.
    undo does nothing if do never ran."""
    sl = os.path.join(target, SETTINGS_LOCAL)
    state = {}

    def save():
        state["old"] = open(sl, "rb").read() if os.path.exists(sl) else None

    def restore():
        if "old" not in state:
            return
        if state["old"] is None:
            if os.path.exists(sl):
                os.remove(sl)
        else:
            with steady(), open(sl, "wb") as f:
                f.write(state["old"])
    return save, restore


def drop_keys(target, name=None, pid=None):
    """Remove only devproc's own entries from settings.local.json."""
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return
    d = read_json(sl)
    changed = False
    if name and name in d.get("extraKnownMarketplaces", {}):
        del d["extraKnownMarketplaces"][name]
        changed = True
    if pid and pid in d.get("enabledPlugins", {}):
        del d["enabledPlugins"][pid]
        changed = True
    if changed:
        write_json(sl, d)


def restore_original_settings(target, m):
    """If settings.local.json existed before install and, without devproc's entries, still says
    what it said then, write back its original bytes (the CLI reformats it and adds empty keys)."""
    orig = m.get("settingsLocalOriginal")
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not isinstance(orig, str) or not os.path.exists(sl):
        return
    try:
        cur, was = read_json(sl), json.loads(orig)
    except ValueError:
        return
    if not isinstance(cur, dict) or not isinstance(was, dict):
        return
    for k in ("enabledPlugins", "extraKnownMarketplaces"):
        if k in cur and cur[k] == {} and k not in was:
            del cur[k]
    if cur == was:
        with steady(), open(sl, "w", newline="") as f:
            f.write(orig)


def dirs_action(plan, target, created):
    """Create the devproc directories; remember which ones did not exist."""
    def do():
        for d in DIRS:
            p = os.path.join(target, d)
            if not os.path.isdir(p):
                os.mkdir(p)
                created.append(d)

    def undo():
        for d in reversed(list(created)):
            p = os.path.join(target, d)
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
            created.remove(d)
    plan.add("create missing directories under .claude/", do, undo)


def copy_action(plan, src, dst, files, origin, target=None, created=None):
    """Copy src over dst. Undo restores the previous bytes (or removes dst if new), also after a
    copy that stopped half way. Folders it has to create are added to created (relative to target)."""
    state = {}

    def do():
        new_dirs, d = [], os.path.dirname(dst)
        while not os.path.isdir(d):
            new_dirs.insert(0, d)
            d = os.path.dirname(d)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        state["new_dirs"] = new_dirs
        if created is not None:
            created.extend(os.path.relpath(d, target) for d in new_dirs)
        state["old"] = open(dst, "rb").read() if os.path.exists(dst) else None
        state["old_rec"] = files.get(dst)
        state["started"] = True
        shutil.copyfile(src, dst)
        files[dst] = {"sha256": sha(dst), "origin": origin}

    def undo():
        if state.get("started"):
            if state["old"] is None:
                if os.path.exists(dst):
                    os.remove(dst)
            else:
                with steady(), open(dst, "wb") as f:
                    f.write(state["old"])
            if state["old_rec"] is None:
                files.pop(dst, None)
            else:
                files[dst] = state["old_rec"]
        for d in reversed(state.get("new_dirs", [])):
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
            if created is not None and os.path.relpath(d, target) in created:
                created.remove(os.path.relpath(d, target))
    plan.add("copy %s -> %s" % (os.path.relpath(src, SRC), dst), do, undo)


def exclude_action(plan, ex_file, target, top, paths, record):
    """Append a marker+line pair per path to ex_file (none already there). record["exclude"] gets the
    file, the lines, newlineBefore (the first line, if a newline had to be added after the file's last
    line first) and fileCreated."""
    rels = ["/" + os.path.relpath(os.path.join(target, p.rstrip("/")), top) + ("/" if p.endswith("/") else "")
            for p in paths]

    def do():
        dir_created = not os.path.isdir(os.path.dirname(ex_file))
        os.makedirs(os.path.dirname(ex_file), exist_ok=True)
        existed = os.path.exists(ex_file)
        old = open(ex_file, "rb").read() if existed else b""
        have = old.decode("utf-8", "replace").split("\n")
        new = [r for r in rels if not any(have[i] == MARK and have[i + 1] == r for i in range(len(have) - 1))]
        nl = bool(old) and not old.endswith(b"\n") and bool(new)
        record["exclude"] = {"file": ex_file, "lines": rels, "newlineBefore": new[0] if nl else None,
                             "fileCreated": not existed, "dirCreated": dir_created}
        with steady(), open(ex_file, "a") as f:
            if nl:
                f.write("\n")  # else the marker would join the last pattern and break it
            for r in new:
                f.write("%s\n%s\n" % (MARK, r))

    def undo():
        remove_exclude(record.get("exclude"))
    plan.add("add to local git exclude %s: %s" % (ex_file, ", ".join(rels)), do, undo)
    return rels


def remove_exclude(ex, only=None):
    """Remove devproc's marker+line pairs (all, or only the lines in `only`). If devproc added a
    newline before a block (newlineBefore) and this call removes that whole block, which runs to the
    end of the file, the newline goes too; a file devproc created and left empty is removed.
    devproc 0.2.0 appended its marker without a newline first, so it can be joined to the project's
    last pattern ("*.log# devproc: ..."): the pattern is kept and the marker split off."""
    if not ex or not os.path.exists(ex["file"]):
        return
    drop = set(ex["lines"] if only is None else only)
    with open(ex["file"], newline="") as f:
        lines = f.read().split("\n")
    keep = [True] * len(lines)
    i = 0
    while i < len(lines):
        if lines[i].endswith(MARK) and i + 1 < len(lines) and lines[i + 1] in drop:
            if lines[i] == MARK:
                keep[i] = False
            else:
                lines[i] = lines[i][:-len(MARK)]  # 0.2.0: joined to the project's last pattern
            keep[i + 1] = False
            i += 2
            continue
        i += 1
    out = [l for l, k in zip(lines, keep) if k]
    nb = ex.get("newlineBefore")
    if nb in drop and lines[-1] == "":
        starts = [j for j in range(len(lines) - 1) if lines[j] == MARK and lines[j + 1] == nb and not keep[j]]
        if starts and not any(keep[starts[-1]:-1]):
            out = [l for l, k in zip(lines[:starts[-1]], keep[:starts[-1]]) if k]
    text = "\n".join(out)
    if ex.get("fileCreated") and text == "":
        os.remove(ex["file"])
        d = os.path.dirname(ex["file"])
        if ex.get("dirCreated") and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
        return
    with steady(), open(ex["file"], "w", newline="") as f:
        f.write(text)


def split_joined_markers_action(plan, ex):
    """devproc 0.2.0 appended its marker without a newline first; after a last line with no newline,
    the marker joined that pattern ("*.log# devproc: ...") and broke it. Split it off again."""
    if not ex or not os.path.exists(ex["file"]):
        return
    with open(ex["file"], newline="") as f:
        text = f.read()
    lines = text.split("\n")
    if not any(l.endswith(MARK) and l != MARK for l in lines):
        return

    def do():
        with steady(), open(ex["file"], "w", newline="") as f:
            f.write("\n".join(l[:-len(MARK)] + "\n" + MARK if l.endswith(MARK) and l != MARK else l for l in lines))

    def undo():
        with steady(), open(ex["file"], "w", newline="") as f:
            f.write(text)
    plan.add("split the devproc marker off the project's last pattern in %s (0.2.0 joined them)" % ex["file"],
             do, undo)


def declared(target, name):
    return name in local_settings(target).get("extraKnownMarketplaces", {})


def leave_old_layout_actions(plan, target, m):
    """Layout 1: take this directory off devproc@devproc and the shared "devproc" marketplace,
    without breaking other directories or installs in another scope that still use it."""
    mine, others = old_records(target)
    by_us = m.get("pluginInstalledByDevproc", True)
    if not by_us:
        if enabled(target, OLD_ID):
            raise Fail("%s is enabled here, but devproc did not install it (the project had it before). "
                       "Disable or uninstall it yourself, then rerun." % OLD_ID)
        return
    prev = registered_path(target, OLD_MARKETPLACE)
    if others:
        for rec in mine:
            d = os.path.realpath(rec.get("projectPath", ""))
            if os.path.isdir(d):
                plan.add("claude plugin uninstall %s --scope local   (in %s)" % (OLD_ID, d),
                         lambda d=d: run(["claude", "plugin", "uninstall", OLD_ID, "--scope", "local", "-y"], cwd=d),
                         lambda d=d: run(["claude", "plugin", "install", OLD_ID, "--scope", "local"], cwd=d))
        if declared(target, OLD_MARKETPLACE):
            plan.add("drop the shared %s marketplace from this directory (kept registered: %d other install%s)"
                     % (OLD_MARKETPLACE, len(others), "" if len(others) == 1 else "s"),
                     lambda: drop_keys(target, name=OLD_MARKETPLACE),
                     lambda: prev and run(["claude", "plugin", "marketplace", "add", prev, "--scope", "local"],
                                          cwd=target))
    elif prev is not None:
        plan.add("claude plugin marketplace remove %s --scope local   (no other install uses it; also removes "
                 "its plugin records)" % OLD_MARKETPLACE,
                 lambda: run(["claude", "plugin", "marketplace", "remove", OLD_MARKETPLACE, "--scope", "local"],
                             cwd=target),
                 lambda: prev and (run(["claude", "plugin", "marketplace", "add", prev, "--scope", "local"], cwd=target),
                                   run(["claude", "plugin", "install", OLD_ID, "--scope", "local"], cwd=target)))
    plan.add("drop %s from %s if still there" % (OLD_ID, SETTINGS_LOCAL),
             lambda: drop_keys(target, name=OLD_MARKETPLACE, pid=OLD_ID))


def drop_empty_settings(target):
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return
    try:
        d = read_json(sl)
    except ValueError:
        return
    if all(v in ({}, [], None) for v in d.values()):
        os.remove(sl)


def settings_stay_without_devproc(target, m):
    """True if settings.local.json holds anything besides devproc's own entries."""
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return False
    try:
        d = read_json(sl)
    except ValueError:
        return True
    d.get("enabledPlugins", {}).pop(m.get("pluginId", OLD_ID), None)
    d.get("extraKnownMarketplaces", {}).pop(m.get("marketplace", OLD_MARKETPLACE), None)
    return any(v not in ({}, [], None) for v in d.values())


def register_actions(plan, target, dist, name, settings_existed):
    """Register this directory's snapshot as its own marketplace and install the plugin from it."""
    pid = plugin_id(name)

    def undo_marketplace():  # undone last: the CLI rewrites settings.local.json
        if registered_path(target, name) is not None:
            remove_marketplace(target, name)
        drop_keys(target, name=name, pid=pid)
        if not settings_existed:
            drop_empty_settings(target)
    plan.add("claude plugin marketplace add %s --scope local" % dist,
             lambda: run(["claude", "plugin", "marketplace", "add", dist, "--scope", "local"], cwd=target),
             undo_marketplace)
    plan.add("claude plugin install %s --scope local" % pid,
             lambda: run(["claude", "plugin", "install", pid, "--scope", "local"], cwd=target),
             lambda: run(["claude", "plugin", "uninstall", pid, "--scope", "local", "-y"], cwd=target, check=False))


def postcheck(target, name, dist, plugin_hash, mk):
    pid = plugin_id(name)
    want = os.path.join(dist, "plugin")
    if not enabled(target, pid):
        raise Fail("post-check: %s not enabled in %s" % (pid, SETTINGS_LOCAL))
    e = list_entry(target, pid)
    if not e or not e.get("enabled"):
        raise Fail("post-check: `claude plugin list` does not show %s enabled for %s" % (pid, target))
    if e.get("errors"):
        raise Fail("post-check: Claude Code reports load errors for %s: %s" % (pid, e["errors"]))
    if e.get("version") != VERSION:
        raise Fail("post-check: installed version %s, expected %s" % (e.get("version"), VERSION))
    if not same_path(e.get("readFromFolder"), want):
        raise Fail("post-check: Claude Code loads %s from %s, expected %s"
                   % (pid, e.get("readFromFolder"), want))
    reg = registered_path(target, name)
    if not same_path(reg, dist):
        raise Fail("post-check: marketplace %s registered at %s, expected %s" % (name, reg, dist))
    if not snapshot_ok(dist, plugin_hash, mk):
        raise Fail("post-check: the snapshot %s differs from the source" % dist)
    guard = os.path.join(want, "scripts", "guard.py")
    r = run([sys.executable, "-I", os.path.join(SRC, "tests", "selftest.py"), guard], check=False)
    if r.returncode != 0:
        raise Fail("post-check: self-test failed on the installed guard %s" % guard)
    say("  ok: post-check: %s %s enabled for this directory, loaded from its own snapshot; "
        "snapshot matches the source; its guard passes self-test" % (pid, VERSION))


def manifest_files(m, target):
    """{absolute path: {"sha256", "origin"}} from a manifest."""
    return {os.path.join(target, k): v for k, v in m["files"].items()}


def seed_upgrade_actions(plan, m, target, files, created):
    for src, rel in seeds():
        dst = os.path.join(target, rel)
        rec = m["files"].get(rel)
        if rec is None:
            if not os.path.exists(dst):
                copy_action(plan, src, dst, files, "seeded", target, created)
            else:
                plan.note("%s exists, not managed: untouched" % rel)
        elif rec["origin"] == "adopted":
            continue
        elif not os.path.exists(dst):
            plan.note("%s was deleted by the project: not restored" % rel)
        elif sha(dst) != rec["sha256"]:
            if sha(dst) != sha(src):
                plan.note("%s evolved in this project: kept. Compare: diff -u %s %s" % (rel, src, dst))
        elif sha(dst) != sha(src):
            copy_action(plan, src, dst, files, "seeded", target, created)


def manifest_action(plan, target, build):
    """Write the manifest from build(); undo restores the previous bytes (or removes it)."""
    mpath = os.path.join(target, MANIFEST)
    old = open(mpath, "rb").read() if os.path.exists(mpath) else None

    def undo():
        if old is None:
            if os.path.exists(mpath):
                os.remove(mpath)
        else:
            with steady(), open(mpath, "wb") as f:
                f.write(old)
    plan.add("write manifest %s" % mpath, lambda: write_json(mpath, build()), undo)


def prune_snapshots(target, keep):
    """Remove everything under DIST except the snapshot directories in keep (absolute paths)."""
    base = os.path.join(target, DIST)
    if not os.path.isdir(base) or os.path.islink(base):
        return
    for n in os.listdir(base):
        p = os.path.join(base, n)
        if not any(same_path(p, k) for k in keep):
            remove_tree(p)


# ------------------------------------------------------------------ commands

def cmd_install(target, apply):
    m = load_manifest(target)
    if m:
        say("devproc %s is already installed in %s. Nothing changed. Use upgrade, repair or status."
            % (m.get("version"), target))
        return 0
    say("preflight")
    preflight(target)
    stray = sorted(k for k, v in local_settings(target).get("enabledPlugins", {}).items()
                   if v and k.split("@")[0] == "devproc")
    if stray:
        raise Fail("%s already enables %s but has no devproc manifest. Remove it first: "
                   "claude plugin uninstall %s --scope local (run in %s)"
                   % (SETTINGS_LOCAL, ", ".join(stray), stray[0], target))
    name = marketplace_name(target)
    left = leftovers(target)
    if registered_path(target, name) is not None:
        left.insert(0, ("marketplace %s registered at %s" % (name, registered_path(target, name)),
                        "claude plugin marketplace remove %s" % name))
    if left:
        raise Fail("%s has devproc leftovers but no manifest (an earlier run was killed?). Remove them, "
                   "then install again:\n%s" % (target, "\n".join("      %s: %s" % l for l in left)))
    plan = Plan(apply)
    files, created, ex_record = {}, [], {}
    dirs_action(plan, target, created)  # undone last, once the settings restore has run
    save, restore = settings_backup(target)
    plan.add("save %s" % SETTINGS_LOCAL, save, restore)  # restores its exact bytes
    for src, rel in seeds():
        dst = os.path.join(target, rel)
        if not os.path.exists(dst):
            copy_action(plan, src, dst, files, "seeded")
        elif sha(dst) == sha(src):
            files[dst] = {"sha256": sha(dst), "origin": "adopted"}
            plan.note("%s already identical to the template: recorded as the project's own "
                      "(never updated or removed by devproc)" % rel)
        else:
            plan.note("%s exists and differs: left untouched, not managed by devproc" % rel)
    g = git_top(target)
    if g:
        ex_file = git_exclude_file(target)
        split_joined_markers_action(plan, {"file": ex_file})  # kept by a 0.2.0 uninstall
        mine = marked_lines(ex_file)
        need = [p for p in EXCLUDE_PATHS if not ignored(target, p)
                or "/" + os.path.relpath(os.path.join(target, p.rstrip("/")), g[0]) + ("/" if p.endswith("/") else "") in mine]
        if need:
            exclude_action(plan, git_exclude_file(target), target, g[0], need, ex_record)
        else:
            plan.note("git already ignores the devproc paths; exclude file untouched")
    else:
        plan.note("not a git work tree: no exclude needed (leak scans need git and will report 'could NOT run')")
    sl = os.path.join(target, SETTINGS_LOCAL)
    settings_existed = os.path.exists(sl)
    original = open(sl, newline="").read() if settings_existed else None
    dist = dist_dir(target, VERSION)
    snapshot_action(plan, dist, name)
    register_actions(plan, target, dist, name, settings_existed)
    phash = tree_hash(PLUGIN)
    mk = expected_marketplace(name)
    manifest_action(plan, target, lambda: {
        "layout": LAYOUT, "version": VERSION, "source": SRC, "pluginHash": phash, "marketplaceJson": mk,
        "marketplace": name, "pluginId": plugin_id(name), "dist": os.path.relpath(dist, target),
        "projectPath": target, "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": {os.path.relpath(k, target): v for k, v in files.items()},
        "createdDirs": list(created),
        "exclude": ex_record.get("exclude"),
        "settingsLocalExisted": settings_existed,
        "settingsLocalOriginal": original,
        "pluginInstalledByDevproc": True})
    plan.add("post-check", lambda: postcheck(target, name, dist, phash, mk))
    say("plan for %s" % target)
    plan.execute()
    if apply:
        say("installed devproc %s in %s. Restart Claude Code sessions in this directory to load it." % (VERSION, target))
    return 0


def file_states(m, target):
    for rel, rec in sorted(m["files"].items()):
        p = os.path.join(target, rel)
        if rec["origin"] == "adopted":
            state = "project's own (adopted)"
        elif not os.path.exists(p):
            state = "missing"
        else:
            state = "as seeded" if sha(p) == rec["sha256"] else "evolved by project"
        say("  %-45s %s" % (rel, state))


def cmd_status(target):
    m = load_manifest(target)
    if not m:
        say("devproc not installed in %s" % target)
        return 1
    say("devproc %s in %s (installed %s, source %s)" % (m["version"], target, m["installedAt"], m.get("source")))
    file_states(m, target)
    if m.get("layout", 1) < LAYOUT:
        on = enabled(target, OLD_ID)
        say("  plugin %s: %s" % (OLD_ID, "enabled" if on else "NOT enabled"))
        say("  installed with the 0.2.0 layout: the plugin runs in place from the source folder %s, "
            "shared with every directory installed from it. Run --upgrade to move this directory "
            "to its own snapshot." % m.get("source"))
        return 0 if on else 4
    say("  plugin %s, snapshot %s" % (m["pluginId"], m["dist"]))
    try:
        probs = health(target, m)
    except Fail as e:
        say("  PROBLEM: the install could not be checked: %s" % e)
        return 4
    for p, fix in probs:
        if not fix.startswith("--"):
            say("  PROBLEM: %s. Fix by hand: %s" % (p, fix))
            continue
        if VERSION != m["version"]:
            fix = "--upgrade"  # repair needs a source at the installed version; upgrade fixes the same
        say("  PROBLEM: %s. Fix: install.sh %s --apply %s" % (p, fix, target))
    if VERSION != m["version"]:
        say("  this source is at %s: install.sh --upgrade --apply %s moves this directory to it" % (VERSION, target))
    elif tree_hash(PLUGIN) != m["pluginHash"]:
        say("  this source's plugin differs from the installed %s under the same version: "
            "raise \"version\" in plugin/.claude-plugin/plugin.json before an upgrade" % VERSION)
    if probs:
        return 4
    say("  ok: snapshot intact, registered for this directory, plugin enabled and loading")
    return 0


def converge_actions(plan, target, m, phash):
    """Bring a layout-2 install to this source's version (the same one, for repair) and fix every
    problem health() finds. Returns (marketplace name, snapshot dir, the manifest to write, and a
    function that adds the tidying steps; the caller adds it after the manifest and the post-check)."""
    dist = dist_dir(target, VERSION)
    old_name, old_pid = m["marketplace"], m["pluginId"]
    old_dist = os.path.join(target, m["dist"])
    relocated = target != m["projectPath"]
    original_gone = relocated and not os.path.exists(m["projectPath"])
    name = marketplace_name(target) if relocated else old_name
    pid = plugin_id(name)
    new_version = m["version"] != VERSION
    reg = registered_path(target, name)
    old_reg = registered_path(target, old_name) if relocated else reg
    later = []  # tidying steps, added after the post-check
    save, restore = settings_backup(target)
    plan.add("save %s" % SETTINGS_LOCAL, save, restore)  # undone last: restores its exact bytes
    if relocated:
        plan.note("this directory was moved or copied from %s: it gets a marketplace of its own, %s. Marketplace "
                  "%s stays the original's%s" % (m["projectPath"], name, old_name,
                                                 "; the original's folder is gone, so it is removed at the end"
                                                 if original_gone and old_reg is not None
                                                 and not os.path.isdir(old_reg) else ""))
        if reg is not None and not same_path(reg, dist):
            raise Fail("a marketplace named %s is already registered (%s); remove it, then rerun" % (name, reg))
        for d in local_records(target, old_pid):
            desc = "claude plugin uninstall %s --scope local   (this directory's record of the old plugin id, in %s)" % (
                old_pid, d)
            do = (lambda d=d: run(["claude", "plugin", "uninstall", old_pid, "--scope", "local", "-y"], cwd=d))
            if old_reg is not None and os.path.isdir(old_reg):
                plan.add(desc, do, lambda d=d: run(["claude", "plugin", "install", old_pid, "--scope", "local"], cwd=d))
            else:
                # its marketplace folder is gone, so it could not be reinstalled on a rollback: tidy it at the end
                later.append((desc, do))
        plan.add("drop %s and %s from %s" % (old_name, old_pid, SETTINGS_LOCAL),
                 lambda: drop_keys(target, name=old_name, pid=old_pid))
    aside = None
    mk = expected_marketplace(name)
    if not new_version and not relocated and snapshot_ok(dist, phash, installed_marketplace(m)):
        mk = installed_marketplace(m)  # intact: kept as it is
    elif snapshot_ok(dist, phash, mk):
        if new_version:
            plan.note("the kept snapshot %s matches this source: reused" % dist)
    else:
        if os.path.lexists(dist):
            aside = "%s.broken-%s" % (dist, time.strftime("%Y%m%d-%H%M%S"))

            def move_aside():  # same parent folder: renaming needs no write bit on dist itself
                os.rename(dist, aside)

            def move_back():
                if os.path.lexists(aside):
                    remove_tree(dist)
                    os.rename(aside, dist)
            plan.add("move the snapshot that does not match aside: %s" % aside, move_aside, move_back)
        snapshot_action(plan, dist, name)
    if not same_path(reg, dist):
        def undo_add(reg=reg):
            if reg is None:
                if registered_path(target, name) is not None:
                    remove_marketplace(target, name)
            elif os.path.isdir(reg):
                run(["claude", "plugin", "marketplace", "add", reg, "--scope", "local"], cwd=target)
                run(["claude", "plugin", "marketplace", "update", name], cwd=target)
                if list_entry(target, pid) is not None:
                    run(["claude", "plugin", "update", pid, "--scope", "local"], cwd=target)
            else:
                raise Fail("marketplace %s was registered at %s, which no longer exists, so it cannot be "
                           "pointed back; it now points at %s" % (name, reg, dist))
        plan.add("claude plugin marketplace add %s --scope local   (%s %s)" % (
            dist, "repoints" if reg is not None else "registers", name),
            lambda: run(["claude", "plugin", "marketplace", "add", dist, "--scope", "local"], cwd=target),
            undo_add)
    rec = None if relocated else list_entry(target, pid)
    install = (lambda: run(["claude", "plugin", "install", pid, "--scope", "local"], cwd=target))
    uninstall = (lambda: run(["claude", "plugin", "uninstall", pid, "--scope", "local", "-y"], cwd=target, check=False))
    if rec is None:
        plan.add("claude plugin install %s --scope local" % pid, install, uninstall)
    elif rec.get("errors"):
        plan.add("claude plugin uninstall %s --scope local   (Claude Code reports load errors for it)" % pid,
                 uninstall, install)
        plan.add("claude plugin install %s --scope local" % pid, install, uninstall)
    elif rec.get("version") != VERSION:
        # undone by the marketplace step's undo, which points back and updates again
        plan.add("claude plugin marketplace update %s" % name,
                 lambda: run(["claude", "plugin", "marketplace", "update", name], cwd=target))
        plan.add("claude plugin update %s --scope local" % pid,
                 lambda: run(["claude", "plugin", "update", pid, "--scope", "local"], cwd=target))
    plan.add("claude plugin enable %s --scope local   (only if it is disabled here)" % pid,
             lambda: enabled(target, pid) or run(["claude", "plugin", "enable", pid, "--scope", "local"], cwd=target))
    ex = current_exclude(target, m) or {}
    g = git_top(target)
    if g:
        need = [p for p in required_ignores(m) if not ignored(target, p) and not tracked(target, p)]
        if need:
            rec_ex = {}
            added = exclude_action(plan, git_exclude_file(target), target, g[0], need, rec_ex)
            ex = {"file": git_exclude_file(target), "lines": sorted(set(ex.get("lines", [])) | set(added)),
                  "newlineBefore": ex.get("newlineBefore"), "fileCreated": bool(ex.get("fileCreated")),
                  "dirCreated": bool(ex.get("dirCreated"))}

            def merge_newline():  # the append above may have had to add a newline first, or the file
                new = rec_ex.get("exclude", {})
                ex["newlineBefore"] = new.get("newlineBefore") or ex["newlineBefore"]
                ex["fileCreated"] = ex["fileCreated"] or bool(new.get("fileCreated"))
                ex["dirCreated"] = ex["dirCreated"] or bool(new.get("dirCreated"))
            plan.add("record the git exclude lines", merge_newline)
    keep = [dist]
    if new_version and not relocated and snapshot_ok(old_dist, m["pluginHash"], installed_marketplace(m)):
        keep.append(old_dist)  # sessions still running loaded it
    m_new = dict(m, version=VERSION, pluginHash=phash, marketplaceJson=mk, marketplace=name, pluginId=pid,
                 dist=os.path.relpath(dist, target), projectPath=target, exclude=ex or None)

    def tidy():
        if aside:
            plan.add("remove the snapshot moved aside", lambda: remove_tree(aside), warn=True)
        if new_version:  # same version: nothing to prune, a kept previous snapshot stays
            plan.add("remove snapshots other than %s" % " and ".join(os.path.relpath(k, target) for k in keep),
                     lambda: prune_snapshots(target, keep), warn=True)
        for desc, do in later:
            plan.add(desc, do, warn=True)
        if original_gone and old_reg is not None and not os.path.isdir(old_reg):
            # no --scope: the name is declared only in the gone folder's settings.local.json, so a
            # local-scope remove from here is refused ("not declared in local settings")
            plan.add("claude plugin marketplace remove %s   (the original's; its folder %s no longer exists)"
                     % (old_name, m["projectPath"]),
                     lambda: run(["claude", "plugin", "marketplace", "remove", old_name], cwd=target),
                     warn=True)
    return name, dist, m_new, tidy


def cmd_upgrade(target, apply):
    m = load_manifest(target)
    if not m:
        raise Fail("devproc not installed in %s; run install first" % target)
    layout = m.get("layout", 1)
    phash = tree_hash(PLUGIN)
    if layout >= LAYOUT and m["version"] == VERSION and phash != m["pluginHash"]:
        raise Fail("this source's plugin changed since %s was installed here, but the version is still "
                   "%s. A version's code never changes: raise \"version\" in %s, then run --upgrade."
                   % (VERSION, VERSION, os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
    say("preflight")
    preflight(target)
    plan = Plan(apply)
    files = manifest_files(m, target)
    created = list(m.get("createdDirs", []))
    seed_upgrade_actions(plan, m, target, files, created)
    if layout >= LAYOUT:
        for p, fix in health(target, m):
            if fix.startswith("--"):
                plan.note("problem, fixed by this run: " + p)
        name, new, m_new, tidy = converge_actions(plan, target, m, phash)
        if m["version"] == VERSION:
            plan.note("plugin already at %s with the same code" % VERSION)
    else:
        # layout 1 -> 2: leave the shared marketplace, give this directory its own snapshot
        name = marketplace_name(target)
        new = dist_dir(target, VERSION)
        tidy = None
        if registered_path(target, name) is not None:
            raise Fail("a marketplace named %s is already registered (%s); remove it, then rerun"
                       % (name, registered_path(target, name)))
        if os.path.lexists(new):
            raise Fail("%s already exists; remove it, then rerun" % new)
        save, restore = settings_backup(target)
        plan.add("save %s" % SETTINGS_LOCAL, save, restore)
        if not os.path.isdir(os.path.join(target, DIST)):
            def mk():
                os.mkdir(os.path.join(target, DIST))
                created.append(DIST)

            def unmk():
                p = os.path.join(target, DIST)
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
                if DIST in created:
                    created.remove(DIST)
            plan.add("create %s" % DIST, mk, unmk)
        snapshot_action(plan, new, name)
        split_joined_markers_action(plan, current_exclude(target, m))
        leave_old_layout_actions(plan, target, m)
        register_actions(plan, target, new, name, True)
        m_new = dict(m, layout=LAYOUT, version=VERSION, pluginHash=phash, marketplace=name,
                     pluginId=plugin_id(name), dist=os.path.relpath(new, target), projectPath=target,
                     pluginInstalledByDevproc=True, marketplaceJson=expected_marketplace(name))

    def build():
        return dict(m_new, upgradedAt=time.strftime("%Y-%m-%dT%H:%M:%S%z"), createdDirs=list(created),
                    files={os.path.relpath(k, target): v for k, v in files.items()})
    manifest_action(plan, target, build)
    plan.add("post-check", lambda: postcheck(target, name, new, phash, m_new["marketplaceJson"]))
    if tidy:
        tidy()
    say("plan for %s" % target)
    plan.execute()
    return 0


def cmd_repair(target, apply):
    m = load_manifest(target)
    if not m:
        raise Fail("devproc not installed in %s; run install first" % target)
    if m.get("layout", 1) < LAYOUT:
        raise Fail("installed with the 0.2.0 layout: run --upgrade, which moves it to its own snapshot")
    if m["version"] != VERSION:
        raise Fail("installed version %s, this source is at %s. Repair restores %s exactly, so it needs a "
                   "source at %s (in a git clone: check out its tag). Or run --upgrade: it moves this "
                   "directory to %s and fixes the same problems." % (m["version"], VERSION, m["version"],
                                                                    m["version"], VERSION))
    phash = tree_hash(PLUGIN)
    if phash != m["pluginHash"]:
        raise Fail("this source's plugin differs from what was installed as %s, so repair cannot restore it. "
                   "Use a source with that release's code (in a git clone: check out its tag), or raise "
                   "the version and run --upgrade." % VERSION)
    probs = health(target, m)
    if not probs:
        say("devproc %s in %s is healthy: nothing to repair." % (VERSION, target))
        return 0
    say("preflight")
    preflight(target)
    plan = Plan(apply)
    for p, _ in probs:
        plan.note("problem: " + p)
    name, dist, m_new, tidy = converge_actions(plan, target, m, phash)
    manifest_action(plan, target, lambda: dict(m_new, repairedAt=time.strftime("%Y-%m-%dT%H:%M:%S%z")))
    plan.add("post-check", lambda: postcheck(target, name, dist, phash, m_new["marketplaceJson"]))
    tidy()
    say("plan for %s" % target)
    plan.execute()
    return 0


def cmd_uninstall(target, apply):
    m = load_manifest(target)
    if not m:
        say("devproc not installed in %s. Nothing changed." % target)
        return 0
    plan = Plan(apply, rollback=False)
    if m.get("layout", 1) < LAYOUT:
        if m.get("pluginInstalledByDevproc", True):
            mine, others = old_records(target)
            if others:
                for rec in mine:
                    d = os.path.realpath(rec.get("projectPath", ""))
                    if os.path.isdir(d):
                        plan.add("claude plugin uninstall %s --scope local   (in %s)" % (OLD_ID, d),
                                 lambda d=d: run(["claude", "plugin", "uninstall", OLD_ID, "--scope", "local", "-y"],
                                                 cwd=d))
                plan.add("drop the shared %s marketplace from this directory (kept registered: %d other install%s)"
                         % (OLD_MARKETPLACE, len(others), "" if len(others) == 1 else "s"),
                         lambda: drop_keys(target, name=OLD_MARKETPLACE, pid=OLD_ID))
            elif registered_path(target, OLD_MARKETPLACE) is not None:
                plan.add("claude plugin marketplace remove %s --scope local   (no other install uses it)" % OLD_MARKETPLACE,
                         lambda: run(["claude", "plugin", "marketplace", "remove", OLD_MARKETPLACE, "--scope", "local"],
                                     cwd=target))
            plan.add("drop %s from %s if still there" % (OLD_ID, SETTINGS_LOCAL),
                     lambda: drop_keys(target, name=OLD_MARKETPLACE, pid=OLD_ID))
    else:
        name, pid = m["marketplace"], m["pluginId"]
        reg = registered_path(target, name)
        ours = reg is not None and (inside(reg, os.path.join(target, DIST)) or (
            not os.path.isdir(reg) and (target == m["projectPath"] or not os.path.exists(m["projectPath"]))))
        if reg is not None and ours:
            plan.add("claude plugin marketplace remove %s --scope local   (also removes its plugin records)" % name,
                     lambda: remove_marketplace(target, name))
        elif reg is not None:
            plan.note("marketplace %s points at %s, not into this directory: left registered" % (name, reg))
            for d in local_records(target, pid):
                plan.add("claude plugin uninstall %s --scope local   (this copy's record, in %s)" % (pid, d),
                         lambda d=d: run(["claude", "plugin", "uninstall", pid, "--scope", "local", "-y"], cwd=d),
                         warn=True)
        plan.add("drop %s and %s from %s if still there" % (name, pid, SETTINGS_LOCAL),
                 lambda: drop_keys(target, name=name, pid=pid))
        plan.add("remove the plugin snapshots in %s" % DIST,
                 lambda: prune_snapshots(target, keep=[]))
    kept = []
    for rel, rec in sorted(m["files"].items()):
        p = os.path.join(target, rel)
        if not os.path.exists(p):
            continue
        if rec["origin"] == "seeded" and sha(p) == rec["sha256"]:
            plan.add("remove %s (unchanged since install)" % rel, lambda p=p: os.remove(p))
        else:
            kept.append(rel)
    for d in (".claude/devproc", ".claude/rules/devproc"):
        for root, subdirs, fs in os.walk(os.path.join(target, d)):
            if os.path.relpath(root, target) == DIST:
                subdirs[:] = []  # snapshots are devproc's, removed above
                continue
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), target)
                if rel not in m["files"] and rel != MANIFEST:
                    kept.append(rel)
    # a settings file the project had before install goes back to how it was, exclude line included;
    # one devproc created stays ignored while it still holds the project's own entries
    settings_will_stay = not m.get("settingsLocalExisted") and settings_stay_without_devproc(target, m)
    ex = current_exclude(target, m)
    if ex:
        # keep the exclude line for any path where files stay behind, so kept task
        # data and evidence are not committed by accident
        top = git_top(target)[0]
        keep_lines = []
        for line in ex["lines"]:
            rel = line.lstrip("/")
            rel_t = os.path.relpath(os.path.join(top, rel.rstrip("/")), target)
            if rel_t == SETTINGS_LOCAL.rstrip("/"):
                if settings_will_stay:
                    keep_lines.append(line)
            elif any(k == rel_t or k.startswith(rel_t + "/") for k in kept):
                keep_lines.append(line)
        drop = [l for l in ex["lines"] if l not in keep_lines]
        if drop:
            plan.add("remove devproc lines from %s: %s" % (ex["file"], ", ".join(drop)),
                     lambda: remove_exclude(ex, only=drop))
        if keep_lines:
            plan.note("git exclude lines kept because files stay behind: %s (in %s). Remove the files, "
                      "then delete those lines and the '%s' line above each." % (", ".join(keep_lines), ex["file"], MARK))
    if m.get("settingsLocalExisted"):
        plan.add("restore %s to its bytes from before install, if the project did not change it" % SETTINGS_LOCAL,
                 lambda: restore_original_settings(target, m))
    else:
        plan.add("remove %s if devproc created it and it is now empty" % SETTINGS_LOCAL,
                 lambda: drop_empty_settings(target))
    plan.add("remove manifest", lambda: os.remove(os.path.join(target, MANIFEST)))

    def prune_dirs():
        for d in reversed(m.get("createdDirs", [])):
            p = os.path.join(target, d)
            if os.path.isdir(p) and not os.path.islink(p) and not os.listdir(p):
                os.rmdir(p)
    plan.add("remove empty directories devproc created", prune_dirs)
    if kept:
        shown = kept[:MAX_LISTED]
        plan.note("kept (changed by the project, or project data): %d file(s)\n      %s%s"
                  % (len(kept), "\n      ".join(shown),
                     "\n      ... %d more" % (len(kept) - len(shown)) if len(kept) > len(shown) else ""))
    say("plan for %s" % target)
    plan.execute()
    return 0


USAGE = """usage: devproc_install.py install|upgrade|repair|uninstall|status [--apply] TARGET_DIR
  dry run unless --apply is given (status never writes).
  exit codes: 0 done (status: healthy), 1 refused or rolled back (status: not installed),
  2 usage, 3 rollback incomplete, uninstall stopped, or unexpected error, 4 status: problems."""


def lock(target):
    """Hold an exclusive lock on the target directory itself (no lock file is written)."""
    fd = os.open(target, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise Fail("another devproc run is working on %s; wait for it to finish" % target)
        raise Fail("could not lock %s: %s" % (target, e))
    return fd


def main(argv):
    args = [a for a in argv if a != "--apply"]
    apply = "--apply" in argv
    if len(args) != 2 or args[0] not in ("install", "upgrade", "repair", "uninstall", "status"):
        say(USAGE)
        return 2
    global CLI_TIMEOUT
    t = os.environ.get("DEVPROC_CLI_TIMEOUT")
    if t is not None:
        if not (t.isascii() and t.isdigit() and 1 <= int(t) <= 86400):
            say("devproc: DEVPROC_CLI_TIMEOUT must be a whole number of seconds, 1 to 86400")
            return 2
        CLI_TIMEOUT = int(t)
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(s) is not signal.SIG_IGN:  # nohup's ignored SIGHUP stays ignored
            signal.signal(s, interrupted)
    try:
        for tool in ("git", "claude"):
            if not shutil.which(tool):
                raise Fail("`%s` not found on PATH" % tool)
        target = resolve_target(args[1], top_only=args[0] in ("install", "upgrade", "repair"))
        if args[0] in ("install", "upgrade", "repair"):
            refuse_unsafe_layout(target, with_seeds=args[0] != "repair")  # repair writes no seeded file
        elif os.path.lexists(os.path.join(target, MANIFEST)):  # reached through a symlink, it is another directory's
            refuse_symlinks(target, with_seeds=False, code=4 if args[0] == "status" else 1)
        if apply and args[0] != "status":
            lock(target)
        if args[0] == "install":
            return cmd_install(target, apply)
        if args[0] == "upgrade":
            return cmd_upgrade(target, apply)
        if args[0] == "repair":
            return cmd_repair(target, apply)
        if args[0] == "uninstall":
            return cmd_uninstall(target, apply)
        return cmd_status(target)
    except Fail as e:
        say("devproc: %s" % e)
        return e.code
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as e:
        say("devproc: unexpected error (%s: %s). Nothing after the last printed step was done; "
            "run --status to see the state." % (type(e).__name__, e))
        return 3
    except KeyboardInterrupt:
        say("devproc: interrupted. Nothing after the last printed step was done; run --status to see the state.")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
