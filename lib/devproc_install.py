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
  - as dpkg: no rollback. The first step writes the manifest, marked pending, with everything
    the run will do; a run that fails, is interrupted or is killed stops part-way, and
    rerunning it finishes it, or uninstall removes it. Every file write is atomic;
  - one run at a time per directory; running install twice changes nothing.
Exit codes: 0 done (status: healthy), 1 refused, nothing changed (status: not installed),
2 usage, 3 stopped part-way (a failed step, an interrupt, an unexpected error): rerun to
finish, 4 status found problems.
stdlib only. devproc itself makes no network calls (the `claude` CLI is Claude Code's own).
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PLUGIN = os.path.join(SRC, "plugin")
TPL = os.path.join(PLUGIN, "templates")
VERSION = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), encoding="utf-8"))["version"]
LAYOUT = 2
MANIFEST = ".claude/devproc/manifest.json"
SETTINGS_LOCAL = ".claude/settings.local.json"
DIST = ".claude/devproc/dist"
EXCLUDE_PATHS = [".claude/devproc/", ".claude/rules/devproc/", SETTINGS_LOCAL]
DIRS = [".claude", ".claude/rules", ".claude/rules/devproc", ".claude/devproc", DIST]
MARK = "# devproc: added by devproc install.sh, removed by uninstall.sh"
# what an uninstall puts in place of MARK above a line it keeps for files left behind: no other
# install's uninstall removes it (it removes only pairs under MARK), and a reinstall in the same
# directory takes it back
KEPT = "# devproc: kept by uninstall.sh for files left in %s; delete this line and the next once they are gone"
# a pending manifest's "previous": the install as it was before the run, which a rerun converges from
PREVIOUS_KEYS = ("version", "pluginHash", "marketplaceJson", "marketplace", "pluginId", "dist", "projectPath")
TMP_TAG = ".devproc-tmp"  # atomic_write's temp files: "." + name + "." + random + TMP_TAG
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
    """Print a line. A gone terminal (ssh dropped, `| head` closed) must not stop a run: on the
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
# ignores SIGHUP). The first only counts: the run stops before its next step (Plan.execute), so it
# ends between two steps. A second one (closing a terminal can send SIGHUP twice) stops it at once.
# Both leave a state a rerun finishes: every write is atomic, and the manifest was written first.
INTERRUPTS = {"n": 0}
# the descriptors holding devproc's locks (lock, lock_config). Each `claude` call runs under HOLD, a
# small Python process that inherits them and closes them for `claude` itself: a call left running when
# devproc stops at once keeps the locks until it ends, so no new run starts beside it, and a daemon that
# `claude` starts (it can start one that lives for days) never holds them.
LOCKS = []
HOLD = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"


def interrupted(signum, frame):
    INTERRUPTS["n"] += 1
    if INTERRUPTS["n"] > 1:
        raise KeyboardInterrupt
    try:  # os.write, not print: a print here could hit the buffered stream mid-write
        os.write(2, b"\ndevproc: interrupted: stopping after the current step (interrupt again to stop at once)\n")
    except OSError:
        pass


def stop(p):
    """Kill p and what it started (its own process group), and reap it."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run(args, cwd=None, check=True):
    env = {k: v for k, v in os.environ.items() if k not in GIT_REPO_VARS}
    if args[0] == "git":
        env.update(LC_ALL="C", LANGUAGE="")  # its messages are matched below ("not a git repository")
    # A session of its own: a terminal's Ctrl-C or hangup reaches devproc only. Output goes to
    # unnamed temp files, not pipes: devproc stopped at once (a second interrupt) leaves the call to
    # finish by itself, which a closed pipe would kill mid-write of the plugin registry every
    # directory shares; and a grandchild holding the output cannot keep devproc waiting.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            argv = [sys.executable, "-I", "-c", HOLD] + args if args[0] == "claude" else args
            p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                 start_new_session=True, pass_fds=LOCKS if args[0] == "claude" else ())
        except OSError as e:
            raise Fail("`%s` could not run: %s" % (" ".join(args), e))
        try:
            p.wait(timeout=CLI_TIMEOUT)
        except subprocess.TimeoutExpired:
            stop(p)
            raise Fail("`%s` did not finish within %d s" % (" ".join(args), CLI_TIMEOUT))
        out.seek(0)
        err.seek(0)
        r = subprocess.CompletedProcess(args, p.returncode, out.read().decode("utf-8", "replace"),
                                        err.read().decode("utf-8", "replace"))
    if check and r.returncode != 0:
        raise Fail("`%s` exit %d: %s" % (" ".join(args), r.returncode,
                                        (r.stderr or r.stdout).strip()[:400]))
    return r


def read_json(path):
    with open(path, encoding="utf-8") as f:  # JSON is UTF-8 (RFC 8259), whatever the locale says
        return json.load(f)


def failpoint(name):
    """Test hook: with DEVPROC_FAILPOINT=<name>, the run SIGKILLs itself here, as kill -9, the OOM
    killer or a power cut would. Used by tests/installer-e2e.sh; does nothing otherwise."""
    if os.environ.get("DEVPROC_FAILPOINT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


def fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Replace path's content with the bytes data: written to a temp file in the same folder, fsynced,
    renamed over path, and the folder fsynced (rename(2), fsync(2)). A kill, a power cut or a full
    disk at any point leaves the old bytes or the new ones, never a cut file. path keeps its mode; a
    new file gets the umask's. A killed run can leave a temp file, which the next run removes.
    A symlink at path is written through, as open() would: the rename replaces the file it points
    to, so a .git/info/exclude kept as a link into a dotfiles repo stays a link."""
    path = os.path.realpath(path)
    d = os.path.dirname(path) or "."
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        um = os.umask(0)
        os.umask(um)
        mode = 0o666 & ~um
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=TMP_TAG)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        failpoint("write:" + os.path.basename(path))
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise
    fsync_dir(d)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


# ------------------------------------------------------------------ plan / apply

class Plan:
    """Ordered steps, with no undo, as dpkg runs them. A step that fails, or an interrupt, stops the
    run after the step it was in (exit 3); the steps before it stay done. Rerunning the same command
    finishes it, so every step must be safe to run again; uninstall removes it, since the first step
    wrote a manifest naming everything the run may do. A step added with warn=True is tidying after
    the work is done, or work that is safe to skip: its failure is reported and the run carries on,
    and an interrupt does not stop the run before it."""

    def __init__(self, apply, uninstall=False):
        self.apply = apply
        self.uninstall = uninstall
        self.steps = []
        self.notes = []

    def add(self, desc, do, warn=False):
        self.steps.append((desc, do, warn))

    def note(self, msg):
        self.notes.append(msg)

    def stopped(self, i, why):
        """The Fail for a run stopped at step i: steps before it are done and kept."""
        done = ("nothing was changed" if i == 1 else "step 1 is done and kept" if i == 2
                else "steps 1 to %d are done and kept" % (i - 1))
        how = ("Rerun the same command to finish it" if self.uninstall else
               "Rerun the same command to finish it, or uninstall.sh --apply to remove it")
        return Fail("%s at step %d/%d; %s. %s: it is safe to rerun." % (why, i, len(self.steps), done, how), 3)

    def execute(self):
        tag = "" if self.apply else "[dry-run] "
        for n in self.notes:
            say("  note: " + n)
        if not self.steps:
            say("  nothing to do")
            return
        for i, (desc, do, warn) in enumerate(self.steps, 1):
            if INTERRUPTS["n"] and not warn:  # a warn step is quick tidying, or work that is safe to skip: it runs
                raise self.stopped(i, "interrupted: stopped before the step")
            say("  %s[%d/%d] %s" % (tag, i, len(self.steps), desc))
            if not self.apply:
                continue
            failpoint("step:%d" % i)  # killed before step i: a rerun or an uninstall must finish it
            try:
                do()
            except Exception as e:
                if warn:
                    say("  warning: this step failed (%s). The work before it is done and kept; "
                        "do this step by hand." % e)
                    continue
                say("  FAILED: %s" % e)
                raise self.stopped(i, "stopped: this step failed (it may be partly done)")
        if self.apply:
            failpoint("step:%d" % (len(self.steps) + 1))
        else:
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


def kept_mark(target):
    return KEPT % target.replace("\n", "?")


def marked_lines(ex_file, target):
    """Lines under a devproc marker in ex_file: devproc's own, from a killed run, or kept for files
    left in target by an earlier uninstall there."""
    if not os.path.exists(ex_file):
        return set()
    l = open(ex_file, encoding="utf-8", errors="surrogateescape", newline="").read().split("\n")
    return {l[i + 1] for i in range(len(l) - 1) if l[i].endswith(MARK) or l[i] == kept_mark(target)}


def kept_for_others(ex_file, target):
    """Lines in ex_file kept by an uninstall of another directory (under its KEPT marker). They make
    git ignore the path, but that directory's owner may delete them at any time: an install here
    adds a pair of its own instead of relying on them."""
    if not os.path.exists(ex_file):
        return set()
    l = open(ex_file, encoding="utf-8", errors="surrogateescape", newline="").read().split("\n")
    head = KEPT.split("%s")[0]
    return {l[i + 1] for i in range(len(l) - 1) if l[i].startswith(head) and l[i] != kept_mark(target)}


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
    if not isinstance(m.get("pending", ""), str):
        wrong.append("pending")
    if not isinstance(m.get("previous", {}), dict) or not all(
            isinstance(m["previous"].get(k), dict if k == "marketplaceJson" else str)
            for k in PREVIOUS_KEYS if k in m.get("previous", {}) and isinstance(m["previous"], dict)):
        wrong.append("previous")
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
    if m.get("pending") == "install":  # finishing an install: every line it set out to add
        return [p for p in EXCLUDE_PATHS if p == ".claude/devproc/" or p.rstrip("/") in lines]
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
    here = lambda ls: ["/" + os.path.relpath(os.path.join(target, l.lstrip("/").rstrip("/")), g[0])
                       + ("/" if l.endswith("/") else "") for l in ls]
    f = git_exclude_file(target)
    out = dict(ex, file=f, lines=here(ex["lines"]), adopted=here(ex.get("adopted", [])))
    if (not isinstance(ex.get("file"), str)
            or (os.path.realpath(ex["file"]) != os.path.realpath(f)
                and os.path.relpath(f, target) != os.path.relpath(ex["file"], m.get("projectPath", target)))):
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
    tmp = stale_temp_files(target)
    if tmp:
        out.append(("%d temp file(s) a killed run left: %s" % (len(tmp), ", ".join(tmp)), "--repair"))
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


def fsync_tree(root):
    """fsync every file and folder under root (symlinks are not followed), so a renamed snapshot is
    complete on disk."""
    for d, dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                fd = os.open(p, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        fsync_dir(d)


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
        fsync_tree(tmp)
        os.rename(tmp, dst)  # rename first: Linux may refuse to move a directory that is not writable
        renamed = True
        fsync_dir(os.path.dirname(dst))
        make_readonly(dst)
    except BaseException:
        remove_tree(tmp)
        if renamed:
            remove_tree(dst)
        raise


def snapshot_action(plan, dst, name):
    def do():
        if os.path.lexists(dst):
            raise Fail("%s already exists" % dst)
        build_snapshot(dst, name)
    plan.add("write read-only plugin snapshot %s (marketplace %s)" % (dst, name), do)


def temp_cleanup_action(plan, target):
    tmp = stale_temp_files(target)
    if tmp:
        plan.add("remove %d temp file(s) a killed run left: %s" % (len(tmp), ", ".join(tmp)),
                 lambda: [os.remove(p) for p in tmp if os.path.lexists(p)])


def stale_temp_files(target):
    """atomic_write temp files a killed run left in the folders devproc writes."""
    dirs = [os.path.join(target, d) for d in (".claude", ".claude/devproc", ".claude/rules/devproc")]
    try:
        ex = git_exclude_file(target) if git_top(target) else None
    except Fail:
        ex = None
    if ex:
        dirs += [os.path.dirname(ex), os.path.dirname(os.path.realpath(ex))]  # atomic_write writes through a link
    return sorted(os.path.join(d, n) for d in dict.fromkeys(dirs) if os.path.isdir(d) and not os.path.islink(d)
                  for n in os.listdir(d) if is_temp(n))


def is_temp(name):
    return name.startswith(".") and name.endswith(TMP_TAG)


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
        atomic_write(sl, orig.encode("utf-8"))


def begin_action(plan, target, start, pending):
    """The first step of every changing run, as dpkg marks a package half-installed before it
    unpacks: create the missing devproc folders (start["mkdirs"]) and write the manifest
    start["manifest"], marked "pending", with every record the run needs. The caller fills start in
    while it plans, before anything runs. A run stopped at any later point is then finished by a
    rerun, or removed by uninstall, from this manifest."""
    def do():
        for d in start.get("mkdirs", []):
            p = os.path.join(target, d)
            if not os.path.isdir(p):
                os.mkdir(p)
        write_json(os.path.join(target, MANIFEST), dict(start["manifest"], pending=pending))
    plan.add("%swrite the manifest first, marked pending (%s)"
             % ("create missing directories under .claude/ and " if pending == "install" else "", pending), do)


def finish_action(plan, target, m):
    """The last work step: the manifest without "pending" and the run's working records."""
    final = {k: v for k, v in m.items() if k not in ("pending", "previous", "oldPluginInstalledByDevproc")}
    final["files"] = {rel: {k: v for k, v in rec.items() if k != "was"} for rel, rec in m["files"].items()}
    plan.add("write the final manifest %s (the run is complete)" % os.path.join(target, MANIFEST),
             lambda: write_json(os.path.join(target, MANIFEST), final))


def unchanged(rec, path):
    """True if the file at path is as devproc left it: its recorded hash, or, while a run that
    copies it is pending, its hash from before that run ("was"; the copy may not have run yet)."""
    h = sha(path)
    return h == rec["sha256"] or h == rec.get("was")


def copy_action(plan, src, rel, target, files, created=None):
    """Copy the template src to target/rel, recorded now, at plan time, with the template's hash and
    "was", the file's hash before the run (None: no file), so the pending manifest matches the file
    whether or not the copy has run. Folders it has to create are added to created."""
    dst = os.path.join(target, rel)
    files[rel] = {"sha256": sha(src), "origin": "seeded", "was": sha(dst) if os.path.exists(dst) else None}
    d = os.path.dirname(rel)
    while created is not None and d and not os.path.isdir(os.path.join(target, d)):
        if d not in created:
            created.append(d)
        d = os.path.dirname(d)

    def do():
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f:
            atomic_write(dst, f.read())
    plan.add("copy %s -> %s" % (os.path.relpath(src, SRC), dst), do)


def exclude_action(plan, ex_file, target, top, paths):
    """Append a marker+line pair per path to ex_file (none already there). Returns the record for the
    manifest, computed now from the file as it is: the file, the lines, newlineBefore (the first
    line, if a newline must be added after the file's last line first), fileCreated, dirCreated."""
    rels = ["/" + os.path.relpath(os.path.join(target, p.rstrip("/")), top) + ("/" if p.endswith("/") else "")
            for p in paths]

    def state():
        old = open(ex_file, "rb").read() if os.path.exists(ex_file) else b""
        have = old.decode("utf-8", "replace").split("\n")
        new = [r for r in rels if not any(have[i] in (MARK, kept_mark(target)) and have[i + 1] == r
                                          for i in range(len(have) - 1))]
        return old, new, bool(old) and not old.endswith(b"\n") and bool(new)
    _, new, nl = state()
    record = {"file": ex_file, "lines": rels, "newlineBefore": new[0] if nl else None,
              "fileCreated": not os.path.exists(ex_file), "dirCreated": not os.path.isdir(os.path.dirname(ex_file))}

    def do():  # safe to rerun: adds only the pairs not there yet
        os.makedirs(os.path.dirname(ex_file), exist_ok=True)
        old, new, nl = state()
        add = b"\n" if nl else b""  # else the marker would join the last pattern and break it
        add += "".join("%s\n%s\n" % (MARK, r) for r in new).encode("utf-8")
        # take back the lines an earlier uninstall here kept for files left behind
        mine = old.decode("utf-8", "surrogateescape").split("\n")
        for i in range(len(mine) - 1):
            if mine[i] == kept_mark(target) and mine[i + 1] in rels:
                mine[i] = MARK
        taken = "\n".join(mine).encode("utf-8", "surrogateescape")
        if add or taken != old:
            atomic_write(ex_file, taken + add)
    plan.add("add to local git exclude %s: %s" % (ex_file, ", ".join(rels)), do)
    return record


def remove_exclude(ex, target, only=None):
    """Remove devproc's marker+line pairs (all, or only the lines in `only`). If devproc added a
    newline before a block (newlineBefore) and this call removes that whole block, which runs to the
    end of the file, the newline goes too; a file devproc created and left empty is removed.
    devproc 0.2.0 appended its marker without a newline first, so it can be joined to the project's
    last pattern ("*.log# devproc: ..."): the pattern is kept and the marker split off."""
    if not ex or not os.path.exists(ex["file"]):
        return
    drop = set(ex["lines"] if only is None else only)
    with open(ex["file"], encoding="utf-8", errors="surrogateescape", newline="") as f:
        lines = f.read().split("\n")
    keep = [True] * len(lines)
    i = 0
    while i < len(lines):
        if (lines[i].endswith(MARK) or lines[i] == kept_mark(target)) and i + 1 < len(lines) and lines[i + 1] in drop:
            if lines[i] in (MARK, kept_mark(target)):
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
        os.remove(os.path.realpath(ex["file"]))
        d = os.path.dirname(ex["file"])
        if ex.get("dirCreated") and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
        return
    atomic_write(ex["file"], text.encode("utf-8", "surrogateescape"))


def mark_kept(ex, keep, target, shared=()):
    """Put kept_mark(target) in place of MARK above each line in keep: lines this uninstall keeps
    for files left behind. Another install sharing the exclude file (a worktree, or a linked file)
    then cannot remove them with its own uninstall. A line another install also relies on (in
    shared) keeps its MARK pair for that install, and gets a kept pair of its own appended. Safe to rerun."""
    if not os.path.exists(ex["file"]):
        return
    with open(ex["file"], encoding="utf-8", errors="surrogateescape", newline="") as f:
        lines = f.read().split("\n")
    out = [kept_mark(target) if l == MARK and i + 1 < len(lines) and lines[i + 1] in keep
           and lines[i + 1] not in shared else l for i, l in enumerate(lines)]
    have = {out[i + 1] for i in range(len(out) - 1) if out[i] == kept_mark(target)}
    add = ["%s\n%s" % (kept_mark(target), l) for l in keep if l in shared and l not in have]
    if add:
        if out and out[-1] == "":
            out.pop()
        out += add + [""]
    if out != lines:
        atomic_write(ex["file"], "\n".join(out).encode("utf-8", "surrogateescape"))


def split_joined_markers_action(plan, ex):
    """devproc 0.2.0 appended its marker without a newline first; after a last line with no newline,
    the marker joined that pattern ("*.log# devproc: ...") and broke it. Split it off again."""
    if not ex or not os.path.exists(ex["file"]):
        return

    def lines():
        with open(ex["file"], encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read().split("\n")
    if not any(l.endswith(MARK) and l != MARK for l in lines()):
        return

    def do():  # reads the file again: safe to rerun
        atomic_write(ex["file"], "\n".join(l[:-len(MARK)] + "\n" + MARK if l.endswith(MARK) and l != MARK else l
                                           for l in lines()).encode("utf-8", "surrogateescape"))
    plan.add("split the devproc marker off the project's last pattern in %s (0.2.0 joined them)" % ex["file"], do)


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
                         lambda d=d: run(["claude", "plugin", "uninstall", OLD_ID, "--scope", "local", "-y"], cwd=d))
        if declared(target, OLD_MARKETPLACE):
            plan.add("drop the shared %s marketplace from this directory (kept registered: %d other install%s)"
                     % (OLD_MARKETPLACE, len(others), "" if len(others) == 1 else "s"),
                     lambda: drop_keys(target, name=OLD_MARKETPLACE))
    elif prev is not None:
        plan.add("claude plugin marketplace remove %s --scope local   (no other install uses it; also removes "
                 "its plugin records)" % OLD_MARKETPLACE,
                 lambda: run(["claude", "plugin", "marketplace", "remove", OLD_MARKETPLACE, "--scope", "local"],
                             cwd=target))
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
    for x in (m, previous(m)):  # previous: a stopped repair or upgrade of a moved directory
        d.get("enabledPlugins", {}).pop(x.get("pluginId", OLD_ID), None)
        d.get("extraKnownMarketplaces", {}).pop(x.get("marketplace", OLD_MARKETPLACE), None)
    if m.get("oldPluginInstalledByDevproc"):  # a stopped migration: the 0.2.0 entries are devproc's too
        d.get("enabledPlugins", {}).pop(OLD_ID, None)
        d.get("extraKnownMarketplaces", {}).pop(OLD_MARKETPLACE, None)
    return any(v not in ({}, [], None) for v in d.values())


def register_actions(plan, target, dist, name):
    """Register this directory's snapshot as its own marketplace and install the plugin from it."""
    pid = plugin_id(name)
    plan.add("claude plugin marketplace add %s --scope local" % dist,
             lambda: run(["claude", "plugin", "marketplace", "add", dist, "--scope", "local"], cwd=target))
    plan.add("claude plugin install %s --scope local" % pid,
             lambda: run(["claude", "plugin", "install", pid, "--scope", "local"], cwd=target))


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


def seed_upgrade_actions(plan, m, target, files, created):
    """Bring the seeded files to this source's templates; files (relative path: record) is updated
    with what the run will leave, for the manifest."""
    for src, rel in seeds():
        dst = os.path.join(target, rel)
        rec = m["files"].get(rel)
        if rec is None:
            if not os.path.exists(dst):
                copy_action(plan, src, rel, target, files, created)
            else:
                plan.note("%s exists, not managed: untouched" % rel)
        elif rec["origin"] == "adopted":
            continue
        elif not os.path.exists(dst):
            if "was" in rec and rec["was"] is None:  # a copy a stopped run planned and had not done
                copy_action(plan, src, rel, target, files, created)
            else:
                plan.note("%s was deleted by the project: not restored" % rel)
        elif not unchanged(rec, dst):
            if sha(dst) != sha(src):
                plan.note("%s evolved in this project: kept. Compare: diff -u %s %s" % (rel, src, dst))
        elif sha(dst) != sha(src):
            copy_action(plan, src, rel, target, files, created)
        else:
            files[rel] = {"sha256": sha(dst), "origin": "seeded"}


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

def unfinished_uninstall(target):
    return Fail("an uninstall did not finish in %s. Finish it first: uninstall.sh --apply %s"
                % (target, shlex.quote(target)))


def cmd_install(target, apply):
    m = load_manifest(target)
    if m and m.get("pending"):
        return cmd_upgrade(target, apply)  # finishes it (or refuses a pending uninstall)
    if m:
        say("devproc %s is already installed in %s. Nothing changed. Use upgrade, repair or status."
            % (m.get("version"), target))
        return 0
    say("preflight")
    preflight(target)
    name = marketplace_name(target)
    left = []
    stray = sorted(k for k, v in local_settings(target).get("enabledPlugins", {}).items()
                   if v and k.split("@")[0] == "devproc")
    left.extend(("%s enables %s" % (SETTINGS_LOCAL, k),
                 "(cd %s && claude plugin uninstall %s --scope local)" % (shlex.quote(target), k)) for k in stray)
    if registered_path(target, name) is not None:
        left.append(("marketplace %s registered at %s" % (name, registered_path(target, name)),
                     "claude plugin marketplace remove %s" % name))
    left.extend(leftovers(target))
    if left:  # devproc 0.2.2 and later write the manifest first, so only an older run leaves these
        raise Fail("%s has devproc leftovers but no manifest (a run of devproc 0.2.1 or older was killed?). "
                   "Remove them, then install again:\n%s" % (target, "\n".join("      %s: %s" % l for l in left)))
    plan = Plan(apply)
    temp_cleanup_action(plan, target)
    mkdirs = [d for d in DIRS if not os.path.isdir(os.path.join(target, d))]
    start = {"mkdirs": mkdirs}
    begin_action(plan, target, start, "install")
    files = {}
    for src, rel in seeds():
        dst = os.path.join(target, rel)
        if not os.path.exists(dst):
            copy_action(plan, src, rel, target, files)
        elif sha(dst) == sha(src):
            files[rel] = {"sha256": sha(dst), "origin": "adopted"}
            plan.note("%s already identical to the template: recorded as the project's own "
                      "(never updated or removed by devproc)" % rel)
        else:
            plan.note("%s exists and differs: left untouched, not managed by devproc" % rel)
    ex = None
    g = git_top(target)
    if g:
        ex_file = git_exclude_file(target)
        split_joined_markers_action(plan, {"file": ex_file})  # kept by a 0.2.0 uninstall
        mine = marked_lines(ex_file, target)
        theirs = kept_for_others(ex_file, target)
        rel = lambda p: "/" + os.path.relpath(os.path.join(target, p.rstrip("/")), g[0]) + ("/" if p.endswith("/") else "")
        need = [p for p in EXCLUDE_PATHS if not ignored(target, p) or rel(p) in mine or rel(p) in theirs]
        if need:
            ex = exclude_action(plan, ex_file, target, g[0], need)
            # lines already there before this install (kept by an earlier uninstall, or a killed
            # run's): their uninstall keeps the settings line while that file stays (cmd_uninstall)
            ex["adopted"] = sorted(rel(p) for p in need if rel(p) in mine)
        else:
            plan.note("git already ignores the devproc paths; exclude file untouched")
    else:
        plan.note("not a git work tree: no exclude needed (leak scans need git and will report 'could NOT run')")
    sl = os.path.join(target, SETTINGS_LOCAL)
    settings_existed = os.path.exists(sl)
    original = open(sl, encoding="utf-8", newline="").read() if settings_existed else None
    dist = dist_dir(target, VERSION)
    snapshot_action(plan, dist, name)
    register_actions(plan, target, dist, name)
    phash = tree_hash(PLUGIN)
    mk = expected_marketplace(name)
    m = {"layout": LAYOUT, "version": VERSION, "source": SRC, "pluginHash": phash, "marketplaceJson": mk,
         "marketplace": name, "pluginId": plugin_id(name), "dist": os.path.relpath(dist, target),
         "projectPath": target, "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         "files": files, "createdDirs": mkdirs, "exclude": ex,
         "settingsLocalExisted": settings_existed, "settingsLocalOriginal": original,
         "pluginInstalledByDevproc": True}
    start["manifest"] = m
    plan.add("post-check", lambda: postcheck(target, name, dist, phash, mk))
    finish_action(plan, target, m)
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
            state = "as seeded" if unchanged(rec, p) else "evolved by project"
        say("  %-45s %s" % (rel, state))


def cmd_status(target):
    m = load_manifest(target)
    if not m:
        say("devproc not installed in %s" % target)
        return 1
    say("devproc %s in %s (installed %s, source %s)" % (m["version"], target, m.get("installedAt"), m.get("source")))
    file_states(m, target)
    if m.get("pending"):
        q = shlex.quote(target)
        say("  PROBLEM: a devproc run (%s) did not finish here. Fix: %s" % (
            m["pending"], "uninstall.sh --apply %s finishes it" % q if m["pending"] == "uninstall" else
            "install.sh --apply %s finishes it; or uninstall.sh --apply %s removes it" % (q, q)))
        return 4
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
    problem health() finds. For a pending run, m is the install as it was before that run (its
    "previous"), with the run's records: the steps it had not done are planned again, the ones it
    had done find nothing to do. Returns (marketplace name, snapshot dir, the manifest the run
    leaves, and a function that adds the tidying steps; the caller adds it after the final manifest)."""
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
    if relocated:
        plan.note("this directory was moved or copied from %s: it gets a marketplace of its own, %s. Marketplace "
                  "%s stays the original's%s" % (m["projectPath"], name, old_name,
                                                 "; the original's folder is gone, so it is removed"
                                                 if original_gone and old_reg is not None
                                                 and not os.path.isdir(old_reg) else ""))
        if reg is not None and not same_path(reg, dist):
            raise Fail("a marketplace named %s is already registered (%s); remove it, then rerun" % (name, reg))
        for d in local_records(target, old_pid):
            desc = "claude plugin uninstall %s --scope local   (this directory's record of the old plugin id, in %s)" % (
                old_pid, d)
            plan.add(desc, lambda d=d: run(["claude", "plugin", "uninstall", old_pid, "--scope", "local", "-y"], cwd=d))
        plan.add("drop %s and %s from %s" % (old_name, old_pid, SETTINGS_LOCAL),
                 lambda: drop_keys(target, name=old_name, pid=old_pid))
        if original_gone and old_reg is not None and not os.path.isdir(old_reg):
            # no --scope: the name is declared only in the gone folder's settings.local.json, so a
            # local-scope remove from here is refused ("not declared in local settings")
            plan.add("claude plugin marketplace remove %s   (the original's; its folder %s no longer exists)"
                     % (old_name, m["projectPath"]),
                     lambda: run(["claude", "plugin", "marketplace", "remove", old_name], cwd=target))
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
            # same parent folder: renaming needs no write bit on dist itself
            plan.add("move the snapshot that does not match aside: %s" % aside, lambda: os.rename(dist, aside))
        snapshot_action(plan, dist, name)
    if not same_path(reg, dist):
        plan.add("claude plugin marketplace add %s --scope local   (%s %s)" % (
            dist, "repoints" if reg is not None else "registers", name),
            lambda: run(["claude", "plugin", "marketplace", "add", dist, "--scope", "local"], cwd=target))
    rec = None if relocated else list_entry(target, pid)
    install = (lambda: run(["claude", "plugin", "install", pid, "--scope", "local"], cwd=target))
    uninstall = (lambda: run(["claude", "plugin", "uninstall", pid, "--scope", "local", "-y"], cwd=target, check=False))
    if rec is None:
        plan.add("claude plugin install %s --scope local" % pid, install)
    elif rec.get("errors"):
        plan.add("claude plugin uninstall %s --scope local   (Claude Code reports load errors for it)" % pid, uninstall)
        plan.add("claude plugin install %s --scope local" % pid, install)
    elif rec.get("version") != VERSION:
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
            new = exclude_action(plan, git_exclude_file(target), target, g[0], need)
            ex = {"file": new["file"], "lines": sorted(set(ex.get("lines", [])) | set(new["lines"])),
                  "newlineBefore": new["newlineBefore"] or ex.get("newlineBefore"),
                  "fileCreated": bool(ex.get("fileCreated")) or new["fileCreated"],
                  "dirCreated": bool(ex.get("dirCreated")) or new["dirCreated"], "adopted": ex.get("adopted", [])}
    keep = [dist]
    if new_version and not relocated and snapshot_ok(old_dist, m["pluginHash"], installed_marketplace(m)):
        keep.append(old_dist)  # sessions still running loaded it
    m_new = dict(m, version=VERSION, pluginHash=phash, marketplaceJson=mk, marketplace=name, pluginId=pid,
                 dist=os.path.relpath(dist, target), projectPath=target, exclude=ex or None)

    def moved_aside():  # this run's, and any a stopped earlier run left
        base = os.path.dirname(dist)
        return [os.path.join(base, n) for n in sorted(os.listdir(base)) if n.startswith(os.path.basename(dist) + ".broken-")
                ] if os.path.isdir(base) and not os.path.islink(base) else []

    def tidy():
        if aside or moved_aside():
            plan.add("remove the snapshot moved aside", lambda: [remove_tree(p) for p in moved_aside()], warn=True)
        if new_version:  # same version: nothing to prune, a kept previous snapshot stays
            plan.add("remove snapshots other than %s" % " and ".join(os.path.relpath(k, target) for k in keep),
                     lambda: prune_snapshots(target, keep), warn=True)
    return name, dist, m_new, tidy


def previous(m):
    """The install as it was before the pending run that wrote m (m itself if none is pending)."""
    return dict(m, **m["previous"]) if m.get("pending") and isinstance(m.get("previous"), dict) else m


def cmd_upgrade(target, apply):
    m = load_manifest(target)
    if not m:
        raise Fail("devproc not installed in %s; run install first" % target)
    if m.get("pending") == "uninstall":
        raise unfinished_uninstall(target)
    if m.get("pending"):
        say("an earlier devproc run (%s) did not finish in %s: finishing it" % (m["pending"], target))
    base = previous(m)
    layout = base.get("layout", 1)
    phash = tree_hash(PLUGIN)
    if layout >= LAYOUT and base["version"] == VERSION and phash != base["pluginHash"]:
        raise Fail("this source's plugin changed since %s was installed here, but the version is still "
                   "%s. A version's code never changes: raise \"version\" in %s, then run --upgrade."
                   % (VERSION, VERSION, os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))
    say("preflight")
    preflight(target)
    plan = Plan(apply)
    temp_cleanup_action(plan, target)
    start = {}
    files = {k: dict(v) for k, v in base["files"].items()}
    created = list(base.get("createdDirs", []))
    tidy = None
    if layout >= LAYOUT:
        if m.get("pending") and m["marketplace"] not in (base["marketplace"], marketplace_name(target)):
            # a stopped repair of a directory moved or copied again since: the name it gave the place it
            # was then. Removed while the old pending manifest still names it, so a kill here repeats it.
            unregister_actions(plan, target, m["marketplace"], m["pluginId"], m["projectPath"])
        begin_action(plan, target, start, m.get("pending") or "upgrade to %s" % VERSION)
        split_joined_markers_action(plan, current_exclude(target, base))  # a stopped 0.2.0 migration's
        seed_upgrade_actions(plan, base, target, files, created)
        for p, fix in health(target, base):
            if fix.startswith("--"):
                plan.note("problem, fixed by this run: " + p)
        if m.get("pending") == "migrate":  # finish leaving the 0.2.0 layout: its steps find nothing once done
            leave_old_layout_actions(plan, target, dict(base, pluginInstalledByDevproc=base.get(
                "oldPluginInstalledByDevproc", True)))
        name, new, m_new, tidy = converge_actions(plan, target, base, phash)
        if base["version"] == VERSION and not m.get("pending"):
            plan.note("plugin already at %s with the same code" % VERSION)
    else:
        # layout 1 -> 2: leave the shared marketplace, give this directory its own snapshot
        name = marketplace_name(target)
        new = dist_dir(target, VERSION)
        if registered_path(target, name) is not None:
            raise Fail("a marketplace named %s is already registered (%s); remove it, then rerun"
                       % (name, registered_path(target, name)))
        if os.path.lexists(new):
            raise Fail("%s already exists; remove it, then rerun" % new)
        if not os.path.isdir(os.path.join(target, DIST)):
            start["mkdirs"] = [DIST]
            created.append(DIST)
        begin_action(plan, target, start, "migrate")
        seed_upgrade_actions(plan, m, target, files, created)
        snapshot_action(plan, new, name)
        split_joined_markers_action(plan, current_exclude(target, m))
        leave_old_layout_actions(plan, target, m)
        register_actions(plan, target, new, name)
        m_new = dict(m, layout=LAYOUT, version=VERSION, pluginHash=phash, marketplace=name,
                     pluginId=plugin_id(name), dist=os.path.relpath(new, target), projectPath=target,
                     pluginInstalledByDevproc=True, marketplaceJson=expected_marketplace(name),
                     oldPluginInstalledByDevproc=m.get("pluginInstalledByDevproc", True))
    m_full = dict(m_new, upgradedAt=time.strftime("%Y-%m-%dT%H:%M:%S%z"), createdDirs=created, files=files)
    start["manifest"] = (dict(m_full, previous={k: base[k] for k in PREVIOUS_KEYS if k in base})
                         if layout >= LAYOUT else m_full)
    plan.add("post-check", lambda: postcheck(target, name, new, phash, m_new["marketplaceJson"]))
    finish_action(plan, target, m_full)
    if tidy:
        tidy()
    say("plan for %s" % target)
    plan.execute()
    return 0


def cmd_repair(target, apply):
    m = load_manifest(target)
    if not m:
        raise Fail("devproc not installed in %s; run install first" % target)
    if m.get("pending"):
        refuse_symlinks(target)  # finishing a pending upgrade copies seeded files: none may lead elsewhere
        return cmd_upgrade(target, apply)  # finishes it (or refuses a pending uninstall)
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
    temp_cleanup_action(plan, target)
    start = {}
    begin_action(plan, target, start, "repair")
    for p, _ in probs:
        plan.note("problem: " + p)
    name, dist, m_new, tidy = converge_actions(plan, target, m, phash)
    m_full = dict(m_new, repairedAt=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    start["manifest"] = dict(m_full, previous={k: m[k] for k in PREVIOUS_KEYS if k in m})
    plan.add("post-check", lambda: postcheck(target, name, dist, phash, m_new["marketplaceJson"]))
    finish_action(plan, target, m_full)
    tidy()
    say("plan for %s" % target)
    plan.execute()
    return 0


def unregister_actions(plan, target, name, pid, project_path):
    """Take marketplace `name` and plugin `pid` off this directory: the marketplace itself if it is
    this directory's (registered inside it, or a dead path left by this directory or by a gone
    original), else only this directory's own records of the plugin."""
    reg = registered_path(target, name)
    ours = reg is not None and (inside(reg, os.path.join(target, DIST)) or (
        not os.path.isdir(reg) and (target == project_path or not os.path.exists(project_path))))
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


def shared_exclude_lines(target, m, ex):
    """Lines of exclude file ex["file"] that another devproc install relies on too: the original's,
    for a copy in the same repository; another worktree's of the same repository, either way round
    (git shares info/exclude between worktrees, each matching it against its own top); or any
    devproc install Claude Code has a record of whose exclude file is the same file (repositories
    whose .git/info/exclude links to one shared file). A folder whose manifest is this directory's
    own, reached through a symlinked .claude, is not another install."""
    out = set()
    others = {m.get("projectPath")}
    if git_top(target):
        wl = run(["git", "-C", target, "worktree", "list", "--porcelain"], check=False).stdout.split("\n")
        others |= {os.path.realpath(l[len("worktree "):]) for l in wl if l.startswith("worktree ")}
    others |= {os.path.realpath(p.get("projectPath", "")) for p in plugin_list(target)
               if str(p.get("id", "")).startswith("devproc@") and p.get("projectPath")}
    for d in others - {target, None}:
        if not os.path.exists(os.path.join(d, MANIFEST)) or same_path(os.path.join(d, MANIFEST), os.path.join(target, MANIFEST)):
            continue
        try:
            o = load_manifest(d)
            oex = current_exclude(d, o)
        except Fail:
            continue
        if oex and same_path(oex["file"], ex["file"]):
            out.update(oex["lines"])
    return out


def empty_own_dirs(target):
    """devproc's own folders that are empty but for each other and temp files: a run killed in its
    first step, or an uninstall killed between removing the manifest and removing them, leaves them
    with no manifest naming them."""
    out = []
    for d in (DIST, ".claude/devproc", ".claude/rules/devproc"):
        p = os.path.join(target, d)
        if os.path.isdir(p) and not os.path.islink(p) and all(os.path.join(d, n) in out or is_temp(n) for n in os.listdir(p)):
            out.append(d)
    return out


def leave_old_layout_uninstall_actions(plan, target, by_us):
    """Layout 1 (devproc 0.2.0): take this directory off devproc@devproc and the shared marketplace."""
    if by_us:
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


def cmd_uninstall(target, apply):
    m = load_manifest(target)
    if not m:
        refuse_symlinks(target, with_seeds=False)  # leftovers reached through a symlink are another directory's
        left = empty_own_dirs(target)
        if not left and not stale_temp_files(target):
            say("devproc not installed in %s. Nothing changed." % target)
            return 0
        plan = Plan(apply, uninstall=True)
        temp_cleanup_action(plan, target)
        if left:
            plan.add("remove the empty devproc folders a killed run left: %s" % ", ".join(left),
                     lambda: [os.rmdir(os.path.join(target, d)) for d in empty_own_dirs(target)])
        say("plan for %s" % target)
        plan.execute()
        return 0
    plan = Plan(apply, uninstall=True)
    temp_cleanup_action(plan, target)
    mpath = os.path.join(target, MANIFEST)
    plan.add("mark the manifest pending (uninstall) first",
             lambda: write_json(mpath, dict(m, pending="uninstall")))
    if m.get("layout", 1) < LAYOUT:
        leave_old_layout_uninstall_actions(plan, target, m.get("pluginInstalledByDevproc", True))
    else:
        if "oldPluginInstalledByDevproc" in m:  # a stopped migration from 0.2.0: it may not have left the old layout
            leave_old_layout_uninstall_actions(plan, target, m["oldPluginInstalledByDevproc"])
        unregister_actions(plan, target, m["marketplace"], m["pluginId"], m["projectPath"])
        before = previous(m)
        if before is not m and before["marketplace"] != m["marketplace"]:
            # a stopped repair of a moved or copied directory: what it held before, the original's name
            unregister_actions(plan, target, before["marketplace"], before["pluginId"], before["projectPath"])
        plan.add("remove the plugin snapshots in %s" % DIST,
                 lambda: prune_snapshots(target, keep=[]))
    kept = []
    for rel, rec in sorted(m["files"].items()):
        p = os.path.join(target, rel)
        if not os.path.exists(p):
            continue
        if rec["origin"] == "seeded" and unchanged(rec, p):
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
                if rel not in m["files"] and rel != MANIFEST and not is_temp(f):
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
                # the line was there before install (an earlier uninstall kept it for this file):
                # it stays with the file, which goes back to how it was
                if settings_will_stay or (m.get("settingsLocalExisted") and line in ex.get("adopted", [])):
                    keep_lines.append(line)
            elif any(k == rel_t or k.startswith(rel_t + "/") for k in kept):
                keep_lines.append(line)
        theirs = shared_exclude_lines(target, m, ex)
        shared = [l for l in ex["lines"] if l in theirs and l not in keep_lines]
        shared_kept = [l for l in keep_lines if l in theirs]
        drop = [l for l in ex["lines"] if l not in keep_lines and l not in shared]
        if drop:
            plan.add("remove devproc lines from %s: %s" % (ex["file"], ", ".join(drop)),
                     lambda: remove_exclude(ex, target, only=drop))
        if keep_lines:
            plan.add("mark the lines kept for files left behind in %s: %s" % (ex["file"], ", ".join(keep_lines)),
                     lambda: mark_kept(ex, keep_lines, target, shared_kept))
            plan.note("git exclude lines kept because files stay behind: %s (in %s). Remove the files, "
                      "then delete those lines and the '%s' line above each."
                      % (", ".join(keep_lines), ex["file"], kept_mark(target)))
        if shared:
            plan.note("git exclude lines kept because another devproc install uses the same exclude file "
                      "(%s): %s" % (ex["file"], ", ".join(shared)))
    if m.get("settingsLocalExisted"):
        plan.add("restore %s to its bytes from before install, if the project did not change it" % SETTINGS_LOCAL,
                 lambda: restore_original_settings(target, m))
    else:
        plan.add("remove %s if devproc created it and it is now empty" % SETTINGS_LOCAL,
                 lambda: drop_empty_settings(target))
    def remove_manifest():
        # with the folders in one step: a run killed between the two would leave empty folders
        # that no later run knows are devproc's
        os.remove(os.path.join(target, MANIFEST))
        failpoint("uninstall:manifest-removed")
        # devproc's own folders go when empty even if unrecorded (a killed earlier run created them)
        for d in [DIST, ".claude/devproc", ".claude/rules/devproc"] + list(reversed(m.get("createdDirs", []))):
            p = os.path.join(target, d)
            if os.path.isdir(p) and not os.path.islink(p) and not os.listdir(p):
                os.rmdir(p)
    plan.add("remove the manifest and the empty directories devproc created", remove_manifest)
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
  exit codes: 0 done (status: healthy), 1 refused, nothing changed (status: not installed),
  2 usage, 3 stopped part-way (rerun the same command to finish), 4 status: problems."""


def lock(target):
    """Hold an exclusive lock on the target directory itself (no lock file is written)."""
    fd = os.open(target, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise Fail("another devproc run is working on %s (or a claude call it left running); wait for it "
                       "to finish" % target)
        raise Fail("could not lock %s: %s" % (target, e))
    LOCKS.append(fd)
    return fd


def lock_config(wait=300):
    """Queue behind other devproc runs before changing Claude Code's user-level plugin registry, which
    every directory shares: an exclusive flock on the config folder itself (no lock file), waiting
    up to `wait` s. The target lock alone would let runs in two directories change it at once."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    if not os.path.isdir(cfg):
        return None
    fd = os.open(cfg, os.O_RDONLY)
    deadline, told = time.monotonic() + wait, False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            LOCKS.append(fd)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise Fail("could not lock %s: %s" % (cfg, e))
        if INTERRUPTS["n"]:
            os.close(fd)
            raise Fail("interrupted while waiting for Claude Code's plugin registry; nothing was changed", 3)
        if not told:
            say("waiting for another devproc run to finish with Claude Code's plugin registry (%s), up to %d s"
                % (cfg, wait))
            told = True
        if time.monotonic() > deadline:
            os.close(fd)
            raise Fail("another devproc run kept Claude Code's plugin registry (%s) busy for %d s; "
                       "rerun when it has finished" % (cfg, wait))
        time.sleep(0.2)


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
            lock_config()
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
            "rerun the same command to finish, or run --status to see the state." % (type(e).__name__, e))
        return 3
    except KeyboardInterrupt:
        say("devproc: stopped at once (interrupted twice). The manifest says what the run was doing: rerun "
            "the same command to finish it, or uninstall.sh --apply to remove it; --status shows the state.")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
