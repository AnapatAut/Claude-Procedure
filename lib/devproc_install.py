#!/usr/bin/env python3
"""devproc installer: install | upgrade | uninstall | status, per target directory.

Dry run by default: prints every action. --apply performs them.
Safety rules:
  - writes only inside TARGET/.claude/, TARGET's local git exclude file (never
    committed), and Claude Code's own plugin registry via the `claude plugin` CLI;
  - never replaces a file it did not write, or one changed since it wrote it; in
    settings.local.json and the exclude file it only adds and removes its own entries;
  - inside git, TARGET must be the top level of the main checkout (Claude Code keeps
    local plugin settings there);
  - the guard self-test must pass, and must fail on a mutant, before anything is written;
  - install and upgrade roll back on a failed step; uninstall stops and can be rerun;
  - running install twice changes nothing.
stdlib only. devproc itself makes no network calls (the `claude` CLI is Claude Code's own).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(SRC, "plugin")
TPL = os.path.join(PLUGIN, "templates")
VERSION = json.load(open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")))["version"]
PLUGIN_ID = "devproc@devproc"
MANIFEST = ".claude/devproc/manifest.json"
SETTINGS_LOCAL = ".claude/settings.local.json"
EXCLUDE_PATHS = [".claude/devproc/", ".claude/rules/devproc/", SETTINGS_LOCAL]
DIRS = [".claude", ".claude/rules", ".claude/rules/devproc", ".claude/devproc"]
MARK = "# devproc: added by devproc install.sh, removed by uninstall.sh"
MAX_LISTED = 20


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


def say(msg):
    print(msg, flush=True)


class Fail(Exception):
    pass


def run(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        raise Fail("`%s` exit %d: %s" % (" ".join(args), r.returncode,
                                        (r.stderr or r.stdout).strip()[:400]))
    return r


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------------ plan / apply

class Plan:
    """Ordered steps. rollback=True: a failed step undoes the completed ones.
    rollback=False (uninstall): a failed step stops the run; rerunning finishes it."""

    def __init__(self, apply, rollback=True):
        self.apply = apply
        self.rollback = rollback
        self.steps = []
        self.notes = []

    def add(self, desc, do, undo=None):
        self.steps.append((desc, do, undo))

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
        for desc, do, undo in self.steps:
            say("  %s%s" % (tag, desc))
            if not self.apply:
                continue
            try:
                do()
                done.append((desc, undo))
            except Exception as e:
                say("  FAILED: %s" % e)
                if not self.rollback:
                    raise Fail("stopped at the failed step; %d step(s) were done and are not undone. "
                               "Fix the cause and rerun the same command: it is safe to rerun." % len(done))
                say("  rolling back %d step(s)" % len(done))
                failed = 0
                for d, u in reversed(done):
                    if not u:
                        continue
                    try:
                        u()
                        say("    undone: " + d)
                    except Exception as e2:
                        failed += 1
                        say("    UNDO FAILED (%s): %s" % (d, e2))
                if failed:
                    raise Fail("rollback INCOMPLETE: %d undo step(s) failed, see UNDO FAILED above" % failed)
                raise Fail("rolled back; nothing from this run is left")
        if not self.apply:
            say("dry run only. Rerun with --apply to do it.")


# ------------------------------------------------------------------ checks

def git_top(target):
    """(top level, main checkout top level) if target is in a git work tree, else None."""
    r = run(["git", "-C", target, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"], check=False)
    if r.returncode != 0:
        return None
    top, gd, cd = r.stdout.split("\n")[:3]
    gd = os.path.realpath(os.path.join(target, gd))
    cd = os.path.realpath(os.path.join(target, cd))
    main = os.path.dirname(cd) if gd != cd else os.path.realpath(top)
    return os.path.realpath(top), main


def resolve_target(path, for_install=False):
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
    if for_install:
        g = git_top(t)
        if g and t != g[1]:
            why = "a linked git worktree" if g[0] != g[1] else "a subdirectory of a git repo"
            raise Fail("%s is %s. Claude Code keeps local plugin settings at the top of the main "
                       "checkout, so install there instead: %s" % (t, why, g[1]))
    return t


def preflight(target):
    if sys.version_info < (3, 8):
        raise Fail("python3 >= 3.8 required")
    for tool in ("git", "claude"):
        if not shutil.which(tool):
            raise Fail("`%s` not found on PATH" % tool)
    guard = os.path.join(PLUGIN, "scripts", "guard.py")
    st = os.path.join(SRC, "tests", "selftest.py")
    r = subprocess.run([sys.executable, "-I", st, guard], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    if r.returncode != 0:
        raise Fail("guard self-test failed (%s):\n%s" % (last, r.stdout))
    say("  ok: guard self-test: %s" % last)
    r = subprocess.run([sys.executable, "-I", st, os.path.join(SRC, "tests", "mutant_noop.py")],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 1:
        raise Fail("self-test did not fail on the mutant guard (exit %d): the test proves nothing" % r.returncode)
    say("  ok: self-test fails on a mutant guard, as it must")
    run(["claude", "plugin", "validate", PLUGIN])
    run(["claude", "plugin", "validate", SRC])
    say("  ok: claude plugin validate (plugin, marketplace)")
    sl = os.path.join(target, SETTINGS_LOCAL)
    if os.path.exists(sl):
        try:
            read_json(sl)
        except ValueError as e:
            raise Fail("%s is not valid JSON (%s); fix it first" % (sl, e))


def load_manifest(target):
    p = os.path.join(target, MANIFEST)
    return read_json(p) if os.path.exists(p) else None


def plugin_enabled(target):
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return False
    try:
        return bool(read_json(sl).get("enabledPlugins", {}).get(PLUGIN_ID))
    except ValueError:
        return False


def plugin_list(target):
    return json.loads(run(["claude", "plugin", "list", "--json"], cwd=target).stdout)


def other_installs(target):
    """projectPath of every other directory where devproc is installed (local scope)."""
    return [p["projectPath"] for p in plugin_list(target)
            if p.get("id") == PLUGIN_ID and p.get("scope") == "local"
            and os.path.realpath(p.get("projectPath", "")) != target]


def git_exclude_file(target):
    # --git-path is relative to the -C dir unless absolute (no --path-format: that needs git 2.31+)
    ex = run(["git", "-C", target, "rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    return os.path.normpath(os.path.join(target, ex))


def ignored(target, rel):
    probe = rel + "probe" if rel.endswith("/") else rel
    return run(["git", "-C", target, "check-ignore", "-q", "--", probe], check=False).returncode == 0


# ------------------------------------------------------------------ actions

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
    plan.add("create missing directories under .claude/", do, undo)


def copy_action(plan, src, dst, files, origin):
    """Copy src over dst. Undo restores the previous bytes (or removes dst if new)."""
    state = {}

    def do():
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        state["old"] = open(dst, "rb").read() if os.path.exists(dst) else None
        state["old_rec"] = files.get(dst)
        shutil.copyfile(src, dst)
        files[dst] = {"sha256": sha(dst), "origin": origin}

    def undo():
        if os.path.exists(dst) and sha(dst) == files.get(dst, {}).get("sha256"):
            if state.get("old") is None:
                os.remove(dst)
            else:
                with open(dst, "wb") as f:
                    f.write(state["old"])
        if state.get("old_rec") is None:
            files.pop(dst, None)
        else:
            files[dst] = state["old_rec"]
    plan.add("copy %s -> %s" % (os.path.relpath(src, SRC), dst), do, undo)


def exclude_action(plan, ex_file, target, top, paths, record):
    rels = ["/" + os.path.relpath(os.path.join(target, p.rstrip("/")), top) + ("/" if p.endswith("/") else "")
            for p in paths]

    def do():
        os.makedirs(os.path.dirname(ex_file), exist_ok=True)
        with open(ex_file, "a") as f:
            for r in rels:
                f.write("%s\n%s\n" % (MARK, r))
        record["exclude"] = {"file": ex_file, "lines": rels}

    def undo():
        remove_exclude(record.get("exclude"))
    plan.add("add to local git exclude %s: %s" % (ex_file, ", ".join(rels)), do, undo)


def remove_exclude(ex, only=None):
    """Remove devproc's marker+line pairs (all, or only the lines in `only`)."""
    if not ex or not os.path.exists(ex["file"]):
        return
    drop = set(ex["lines"] if only is None else only)
    with open(ex["file"]) as f:
        lines = f.read().split("\n")
    out, i = [], 0
    while i < len(lines):
        if lines[i] == MARK and i + 1 < len(lines) and lines[i + 1] in drop:
            i += 2
            continue
        out.append(lines[i])
        i += 1
    with open(ex["file"], "w") as f:
        f.write("\n".join(out))


def drop_declaration(target):
    """Remove only devproc's marketplace declaration from settings.local.json."""
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return
    d = read_json(sl)
    if "devproc" in d.get("extraKnownMarketplaces", {}):
        del d["extraKnownMarketplaces"]["devproc"]
        write_json(sl, d)


def remove_marketplace(target):
    """Drop devproc's marketplace for this directory without breaking other directories:
    `claude plugin marketplace remove` also drops the user-level registry entry."""
    if other_installs(target):
        drop_declaration(target)
    else:
        run(["claude", "plugin", "marketplace", "remove", "devproc", "--scope", "local"], cwd=target)


def declared(target):
    sl = os.path.join(target, SETTINGS_LOCAL)
    try:
        return "devproc" in read_json(sl).get("extraKnownMarketplaces", {})
    except (OSError, ValueError):
        return False


def settings_stay_without_devproc(target):
    """True if settings.local.json holds anything besides devproc's own entries."""
    sl = os.path.join(target, SETTINGS_LOCAL)
    if not os.path.exists(sl):
        return False
    try:
        d = read_json(sl)
    except ValueError:
        return True
    d.get("enabledPlugins", {}).pop(PLUGIN_ID, None)
    d.get("extraKnownMarketplaces", {}).pop("devproc", None)
    return any(v not in ({}, [], None) for v in d.values())


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


def plugin_actions(plan, target, settings_existed):
    def undo_marketplace():  # undone last: the CLI rewrites settings.local.json
        remove_marketplace(target)
        if not settings_existed:
            drop_empty_settings(target)
    plan.add("claude plugin marketplace add %s --scope local   (in %s)" % (SRC, target),
             lambda: run(["claude", "plugin", "marketplace", "add", SRC, "--scope", "local"], cwd=target),
             undo_marketplace)
    plan.add("claude plugin install %s --scope local" % PLUGIN_ID,
             lambda: run(["claude", "plugin", "install", PLUGIN_ID, "--scope", "local"], cwd=target),
             lambda: run(["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", "local", "-y"], cwd=target))


def postcheck(target):
    if not plugin_enabled(target):
        raise Fail("post-check: %s not enabled in %s" % (PLUGIN_ID, SETTINGS_LOCAL))
    mine = [p for p in plugin_list(target) if p.get("id") == PLUGIN_ID and p.get("scope") == "local"
            and os.path.realpath(p.get("projectPath", "")) == target]
    if not mine or not mine[0].get("enabled"):
        raise Fail("post-check: `claude plugin list` does not show %s enabled for %s" % (PLUGIN_ID, target))
    if mine[0].get("version") != VERSION:
        raise Fail("post-check: installed version %s, expected %s" % (mine[0].get("version"), VERSION))
    guard = os.path.join(mine[0]["installPath"], "scripts", "guard.py")
    r = subprocess.run([sys.executable, "-I", os.path.join(SRC, "tests", "selftest.py"), guard],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise Fail("post-check: self-test failed on the installed guard %s" % guard)
    say("  ok: post-check: %s %s enabled for this directory; installed guard passes self-test" % (PLUGIN_ID, VERSION))


def manifest_files(m, target):
    """{absolute path: {"sha256", "origin"}} from a manifest."""
    return {os.path.join(target, k): v for k, v in m["files"].items()}


# ------------------------------------------------------------------ commands

def cmd_install(target, apply):
    m = load_manifest(target)
    if m:
        say("devproc %s is already installed in %s. Nothing changed. Use upgrade or status." % (m.get("version"), target))
        return 0
    say("preflight")
    preflight(target)
    plan = Plan(apply)
    files, created, ex_record = {}, [], {}
    dirs_action(plan, target, created)
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
        need = [p for p in EXCLUDE_PATHS if not ignored(target, p)]
        if need:
            exclude_action(plan, git_exclude_file(target), target, g[0], need, ex_record)
        else:
            plan.note("git already ignores the devproc paths; exclude file untouched")
    else:
        plan.note("not a git work tree: no exclude needed (leak scans need git and will report 'could NOT run')")
    settings_existed = os.path.exists(os.path.join(target, SETTINGS_LOCAL))
    if plugin_enabled(target):
        plan.note("%s already enabled here: plugin step skipped" % PLUGIN_ID)
        by_us = False
    else:
        plugin_actions(plan, target, settings_existed)
        by_us = True

    def write_manifest():
        write_json(os.path.join(target, MANIFEST), {
            "version": VERSION, "source": SRC, "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": {os.path.relpath(k, target): v for k, v in files.items()},
            "createdDirs": created,
            "exclude": ex_record.get("exclude"),
            "settingsLocalExisted": settings_existed,
            "pluginInstalledByDevproc": by_us})
    plan.add("write manifest %s" % os.path.join(target, MANIFEST), write_manifest,
             lambda: os.path.exists(os.path.join(target, MANIFEST)) and os.remove(os.path.join(target, MANIFEST)))
    if apply:
        plan.add("post-check", lambda: postcheck(target))
    say("plan for %s" % target)
    plan.execute()
    if apply:
        say("installed devproc %s in %s. Restart Claude Code sessions in this directory to load it." % (VERSION, target))
    return 0


def cmd_status(target):
    m = load_manifest(target)
    if not m:
        say("devproc not installed in %s" % target)
        return 1
    say("devproc %s in %s (installed %s, source %s)" % (m["version"], target, m["installedAt"], m["source"]))
    for rel, rec in sorted(m["files"].items()):
        p = os.path.join(target, rel)
        if rec["origin"] == "adopted":
            state = "project's own (adopted)"
        elif not os.path.exists(p):
            state = "missing"
        else:
            state = "as seeded" if sha(p) == rec["sha256"] else "evolved by project"
        say("  %-45s %s" % (rel, state))
    say("  plugin %s: %s" % (PLUGIN_ID, "enabled" if plugin_enabled(target) else "NOT enabled"))
    if VERSION != m["version"]:
        say("  source is at %s: run install.sh --upgrade to update" % VERSION)
    return 0


def cmd_upgrade(target, apply):
    m = load_manifest(target)
    if not m:
        raise Fail("devproc not installed in %s; run install first" % target)
    say("preflight")
    preflight(target)
    plan = Plan(apply)
    files = manifest_files(m, target)
    for src, rel in seeds():
        dst = os.path.join(target, rel)
        rec = m["files"].get(rel)
        if rec is None:
            if not os.path.exists(dst):
                copy_action(plan, src, dst, files, "seeded")
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
            copy_action(plan, src, dst, files, "seeded")
    # re-adding is idempotent and repairs a registry entry another uninstall removed
    plan.add("claude plugin marketplace add %s --scope local" % SRC,
             lambda: run(["claude", "plugin", "marketplace", "add", SRC, "--scope", "local"], cwd=target))
    plan.add("claude plugin marketplace update devproc",
             lambda: run(["claude", "plugin", "marketplace", "update", "devproc"], cwd=target))
    plan.add("claude plugin update %s --scope local   (not undone on failure: rerun upgrade)" % PLUGIN_ID,
             lambda: run(["claude", "plugin", "update", PLUGIN_ID, "--scope", "local"], cwd=target))
    mpath = os.path.join(target, MANIFEST)
    old_manifest = open(mpath, "rb").read()

    def write_manifest():
        m2 = dict(m, version=VERSION, upgradedAt=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  files={os.path.relpath(k, target): v for k, v in files.items()})
        write_json(mpath, m2)

    def restore_manifest():
        with open(mpath, "wb") as f:
            f.write(old_manifest)
    plan.add("update manifest", write_manifest, restore_manifest)
    if apply:
        plan.add("post-check", lambda: postcheck(target))
    say("plan for %s" % target)
    plan.execute()
    return 0


def cmd_uninstall(target, apply):
    m = load_manifest(target)
    if not m:
        say("devproc not installed in %s. Nothing changed." % target)
        return 0
    plan = Plan(apply, rollback=False)
    by_us = m.get("pluginInstalledByDevproc", True)
    if by_us and plugin_enabled(target):
        plan.add("claude plugin uninstall %s --scope local" % PLUGIN_ID,
                 lambda: run(["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", "local", "-y"], cwd=target))
    if by_us and declared(target):
        n = len(other_installs(target))
        plan.add("remove the devproc marketplace for this directory%s"
                 % (" (kept registered: used by %d other director%s)" % (n, "y" if n == 1 else "ies") if n else ""),
                 lambda: remove_marketplace(target))
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
        for root, _, fs in os.walk(os.path.join(target, d)):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), target)
                if rel not in m["files"] and rel != MANIFEST:
                    kept.append(rel)
    settings_will_stay = m.get("settingsLocalExisted") or settings_stay_without_devproc(target)
    ex = m.get("exclude")
    if ex:
        # keep the exclude line for any path where files stay behind, so kept task
        # data and evidence are not committed by accident
        keep_lines = []
        for line in ex["lines"]:
            rel = line.lstrip("/")
            g = git_top(target)
            rel_t = os.path.relpath(os.path.join(g[0], rel.rstrip("/")), target) if g else rel.rstrip("/")
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
    if not m.get("settingsLocalExisted"):
        plan.add("remove %s if devproc created it and it is now empty" % SETTINGS_LOCAL,
                 lambda: drop_empty_settings(target))
    plan.add("remove manifest", lambda: os.remove(os.path.join(target, MANIFEST)))

    def prune_dirs():
        for d in reversed(m.get("createdDirs", [])):
            p = os.path.join(target, d)
            if os.path.isdir(p) and not os.listdir(p):
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


USAGE = """usage: devproc_install.py install|upgrade|uninstall|status [--apply] TARGET_DIR
  dry run unless --apply is given (status never writes)."""


def main(argv):
    args = [a for a in argv if a != "--apply"]
    apply = "--apply" in argv
    if len(args) != 2 or args[0] not in ("install", "upgrade", "uninstall", "status"):
        say(USAGE)
        return 2
    try:
        target = resolve_target(args[1], for_install=args[0] == "install")
        if args[0] == "install":
            return cmd_install(target, apply)
        if args[0] == "upgrade":
            return cmd_upgrade(target, apply)
        if args[0] == "uninstall":
            return cmd_uninstall(target, apply)
        return cmd_status(target)
    except Fail as e:
        say("devproc: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
