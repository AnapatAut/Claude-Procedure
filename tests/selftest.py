#!/usr/bin/env python3
"""Self-test for plugin/scripts/guard.py (P9: a check that cannot fail proves nothing).

usage: selftest.py GUARD_SCRIPT

Builds throwaway git repos with a local bare remote in a temp dir, feeds the
guard planted hook events, and compares each decision with the expected one.
Exit 0 = every case matched. Exit 1 = a mismatch. The installer also runs this
against tests/mutant_noop.py and requires exit 1, which proves the test can fail.
Nothing outside the temp dir is touched; no network.
"""
import json
import os
import pwd
import subprocess
import sys
import tempfile

GUARD = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
ENV.pop("CLAUDE_PROJECT_DIR", None)
# a caller's variables naming a repository (a git hook, vcsh, yadm) would point every git call
# here at that repository
for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
          "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_GRAFT_FILE",
          "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_CONFIG"):
    ENV.pop(k, None)
# planted values are built at run time so this file holds no literal secret
FAKE_KEY = "AKIA" + "Q" * 16
FAKE_PRIVATE = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_PAT = "github" + "_pat_" + "A1b2" * 8
FAKE_ASIA = "AS" + "IA" + "Z" * 16


def sh(*args, cwd=None):
    subprocess.run(list(args), cwd=cwd, env=ENV, check=True, capture_output=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_repo(root, name, remote, files=None, unpushed=None, config=None):
    d = os.path.join(root, name)
    sh("git", "clone", "-q", remote, d)
    write(os.path.join(d, "README"), "base\n")
    sh("git", "add", "README", cwd=d)
    sh("git", "commit", "-qm", "base", cwd=d)
    sh("git", "push", "-q", "origin", "HEAD:refs/heads/" + name, cwd=d)
    sh("git", "branch", "-q", "--set-upstream-to=origin/" + name, cwd=d)
    for rel, text in (unpushed or {}).items():
        write(os.path.join(d, rel), text)
        sh("git", "add", rel, cwd=d)
    if unpushed:
        sh("git", "commit", "-qm", "work", cwd=d)
    for rel, text in (files or {}).items():
        write(os.path.join(d, rel), text)
    if config is not None:
        write(os.path.join(d, ".claude", "devproc", "config.json"), json.dumps(config))
    return d


def run_guard(event):
    r = subprocess.run([sys.executable, "-I", GUARD], input=json.dumps(event) if not isinstance(event, str) else event,
                       capture_output=True, text=True, env=ENV, timeout=60)
    if r.returncode != 0:
        return "EXIT%d" % r.returncode, r.stderr.strip()[:200]
    out = r.stdout.strip()
    if not out:
        return None, ""
    try:
        h = json.loads(out)["hookSpecificOutput"]
        assert h["hookEventName"] == "PreToolUse"
        return h["permissionDecision"], h.get("permissionDecisionReason", "")
    except Exception as e:
        return "BADJSON", "%s: %s" % (e, out[:200])


def bash(cmd, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": cmd}, "cwd": cwd}


def main():
    if not GUARD or not os.path.isfile(GUARD):
        print(__doc__)
        return 2
    tmp = tempfile.mkdtemp(prefix="devproc-selftest-")
    remote = os.path.join(tmp, "remote.git")
    sh("git", "init", "-q", "--bare", remote)
    clean = make_repo(tmp, "clean", remote, unpushed={"a.txt": "hello\n"})
    sh("git", "config", "alias.p", "push", cwd=clean)
    sh("git", "config", "alias.st", "status", cwd=clean)
    leaky = make_repo(tmp, "leaky", remote, unpushed={"cfg.py": "KEY = '%s'\n" % FAKE_KEY})
    pkey = make_repo(tmp, "pkey", remote, unpushed={"id": FAKE_PRIVATE + "\n"})
    custom = make_repo(tmp, "custom", remote, unpushed={"n.md": "host is corp-build-07\n"},
                       config={"leakPatterns": {"local-host": r"\bcorp-build-[0-9]+\b"}})
    ignored = make_repo(tmp, "ignored", remote, unpushed={"fixtures/k.txt": FAKE_KEY + "\n"},
                        config={"leakIgnorePaths": ["fixtures/*"]})
    badcfg = make_repo(tmp, "badcfg", remote, unpushed={"b.txt": "x\n"})
    write(os.path.join(badcfg, ".claude", "devproc", "config.json"), "{not json")
    worktree = make_repo(tmp, "worktree", remote, files={"README": "base\n" + FAKE_KEY + "\n"})
    # commit with a secret already on origin; a second remote "pub" never fetched
    two = make_repo(tmp, "two", remote, unpushed={"s.py": "K = '%s'\n" % FAKE_KEY})
    sh("git", "push", "-q", "origin", "HEAD:refs/heads/two", cwd=two)
    pub = os.path.join(tmp, "pub.git")
    sh("git", "init", "-q", "--bare", pub)
    sh("git", "remote", "add", "pub", pub, cwd=two)
    # secret added only in a merge resolution ("evil merge")
    evil = make_repo(tmp, "evil", remote)
    sh("git", "checkout", "-qb", "side", cwd=evil)
    write(os.path.join(evil, "x.txt"), "x\n"); sh("git", "add", "x.txt", cwd=evil); sh("git", "commit", "-qm", "x", cwd=evil)
    sh("git", "checkout", "-q", "-", cwd=evil)
    write(os.path.join(evil, "y.txt"), "y\n"); sh("git", "add", "y.txt", cwd=evil); sh("git", "commit", "-qm", "y", cwd=evil)
    sh("git", "merge", "-q", "--no-ff", "--no-commit", "side", cwd=evil)
    write(os.path.join(evil, "README"), "base\n" + FAKE_KEY + "\n"); sh("git", "add", "README", cwd=evil)
    sh("git", "commit", "-qm", "merge", cwd=evil)
    pat = make_repo(tmp, "pat", remote, unpushed={"t.txt": FAKE_PAT + "\n"})
    asia = make_repo(tmp, "asia", remote, unpushed={"t.txt": FAKE_ASIA + "\n"})
    try:
        me = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        me = os.environ.get("USER") or ""
    homep = make_repo(tmp, "homep", remote, unpushed={"p.txt": "log in /home/%s/app\n" % me})
    macp = make_repo(tmp, "macp", remote, unpushed={"p.txt": "log in /Users/%s/app\n" % me})
    relp = make_repo(tmp, "relp", remote, unpushed={"p.txt": "see app/home/%s/x\n" % me})
    listpat = make_repo(tmp, "listpat", remote, unpushed={"b.txt": "x\n"}, config={"leakPatterns": ["ab"]})
    strign = make_repo(tmp, "strign", remote, unpushed={"k.txt": FAKE_KEY + "\n"},
                       config={"leakIgnorePaths": "fixtures/*"})
    badrx = make_repo(tmp, "badrx", remote, unpushed={"b.txt": "x\n"}, config={"leakPatterns": {"broken": "("}})
    untracked = make_repo(tmp, "untracked", remote)
    write(os.path.join(untracked, "new.txt"), FAKE_KEY + "\n")
    nogit = os.path.join(tmp, "nogit"); os.makedirs(nogit)
    write(os.path.join(clean, "m_write.sql"), "create table t (id int);\n")
    write(os.path.join(clean, "m_read.sql"), "select 1;\n")

    A, D, N = "ask", "deny", None
    cases = [
        # push, any spelling -> ask (clean repo)
        ("push plain", bash("git push", clean), A),
        ("push -C", bash("git -C . push", clean), A),
        ("push abs path", bash("/usr/bin/git push origin HEAD", clean), A),
        ("push -c opt", bash("git -c core.pager=cat push", clean), A),
        ("push env + --no-pager", bash("env GIT_TRACE=0 git --no-pager push", clean), A),
        ("push after &&", bash("git status && git push", clean), A),
        ("push in subshell", bash("(cd . && git push)", clean), A),
        ("push via bash -c", bash("bash -c 'git push'", clean), A),
        ("push via ssh", bash("ssh box 'cd r && git push'", clean), A),
        ("push in $()", bash("echo $(git push)", clean), A),
        ("push in backticks", bash("echo `git push`", clean), A),
        ("push via eval", bash("eval git push", clean), A),
        ("push via alias", bash("git p", clean), A),
        ("push multi-line", bash("git add .\ngit push", clean), A),
        ("push unbalanced quote", bash("git push 'oops", clean), A),
        ("push --force-with-lease", bash("git push --force-with-lease origin HEAD", clean), A),
        # leak scan -> deny
        ("leak aws key", bash("git push", leaky), D),
        ("leak private key", bash("git push", pkey), D),
        ("leak custom pattern", bash("git push", custom), D),
        ("leak -C resolves repo", bash("git -C %s push" % leaky, clean), D),
        ("leak ignored path", bash("git push", ignored), A),
        ("bad config -> ask", bash("git push", badcfg), A),
        ("-C to clean from leaky", bash("git -C %s push" % clean, leaky), A),
        # not a push -> silent
        ("git status", bash("git status", clean), N),
        ("git alias to status", bash("git st", clean), N),
        ("git log --grep push", bash("git log --grep push", clean), N),
        ("grep for push", bash("grep -rn 'push' .", clean), N),
        ("echo text", bash("echo hello", clean), N),
        ("npm build", bash("npm run build", clean), N),
        ("git commit", bash("git commit -m fix", clean), N),
        # DB
        ("psql select", bash("psql -c 'select 1'", clean), N),
        ("psql drop", bash("psql -c 'DROP TABLE x'", clean), A),
        ("psql --command=", bash("psql --command='delete from t'", clean), A),
        ("psql -f write file", bash("psql -f m_write.sql", clean), A),
        ("psql -f read file", bash("psql -f m_read.sql", clean), N),
        ("psql -f missing", bash("psql -f nope.sql", clean), A),
        ("psql < write file", bash("psql < m_write.sql", clean), A),
        ("psql interactive", bash("psql", clean), A),
        ("psql heredoc", bash("psql <<'EOF'\nupdate t set a=1;\nEOF", clean), A),
        ("sqlite read", bash("sqlite3 x.db 'select * from t'", clean), N),
        ("sqlite write", bash("sqlite3 x.db 'delete from t'", clean), A),
        ("dropdb", bash("dropdb foo", clean), A),
        ("supabase db push", bash("supabase db push", clean), A),
        ("prisma migrate", bash("bunx prisma migrate deploy", clean), A),
        ("npm db:push", bash("npm run db:push", clean), A),
        ("db via ssh", bash("ssh box \"psql -c 'truncate t'\"", clean), A),
        ("mcp sql write", {"tool_name": "mcp__supabase__execute_sql",
                           "tool_input": {"query": "delete from x"}, "cwd": clean}, A),
        ("mcp sql read", {"tool_name": "mcp__supabase__execute_sql",
                          "tool_input": {"query": "select 1"}, "cwd": clean}, N),
        ("mcp non-db tool", {"tool_name": "mcp__github__create_issue",
                             "tool_input": {"body": "drop table"}, "cwd": clean}, N),
        # robustness
        # bypasses found in review: must now deny (local) or say the scan was NOT run
        ("leak via bash -c", bash("bash -c 'git push'", leaky), D),
        ("leak via sh -lc", bash('sh -lc "git push"', leaky), D),
        ("leak via backticks", bash("echo `git push`", leaky), D),
        ("leak after cd", bash("cd %s && git push" % leaky, clean), D),
        ("leak cd in subshell", bash("(cd %s; git push)" % leaky, clean), D),
        ("subshell cd ends at )", bash("(cd %s); git push" % leaky, clean), A, "clean"),
        ("GIT_DIR= -> not run", bash("GIT_DIR=%s/.git git push" % leaky, clean), A, "NOT run"),
        ("--git-dir -> not run", bash("git --git-dir=%s/.git push" % leaky, clean), A, "NOT run"),
        ("ssh unquoted -> remote", bash("ssh box git push", leaky), A, "another machine"),
        ("quoted text -> not run", bash("git commit -m 'do not git push yet'", clean), A, "NOT run"),
        ("second remote: scanned", bash("git push pub HEAD", two), D),
        ("already on origin: ask", bash("git push origin HEAD", two), A, "no commits"),
        ("secret in merge only", bash("git push", evil), D),
        ("github_pat_ token", bash("git push", pat), D),
        ("ASIA key id", bash("git push", asia), D),
        ("Linux home path", bash("git push", homep), D, "home-path"),
        ("macOS home path", bash("git push", macp), D, "home-path"),
        ("relative .../home/<me> is not a home path", bash("git push", relp), A, "leak scan clean"),
        ("leakPatterns as a list -> could NOT run", bash("git push", listpat), A, "could NOT run"),
        ("leakIgnorePaths as a string -> could NOT run", bash("git push", strign), A, "could NOT run"),
        ("bad leak regex -> ask", bash("git push", badrx), A, "could NOT run"),
        ("clean reason says clean", bash("git push", clean), A, "leak scan clean"),
        # DB false positives and new clients
        ("psql replace() fn", bash("psql -c \"select replace(name,'a','b') from t\"", clean), N),
        ("psql where = 'set'", bash("psql -c \"select * from t where status='set'\"", clean), N),
        ("psql update set", bash("psql -c 'update t set a=1'", clean), A),
        ("redis set", bash("redis-cli set k v", clean), A),
        ("redis get", bash("redis-cli get k", clean), N),
        ("mongo insertOne", bash("mongosh --eval 'db.x.insertOne({a:1})'", clean), A),
        ("mongo find", bash("mongosh --eval 'db.x.find()'", clean), N),
        ("mcp db server", {"tool_name": "mcp__db__run",
                           "tool_input": {"stmt": "drop table x"}, "cwd": clean}, A),
        ("mcp docs query tool", {"tool_name": "mcp__wiki__query",
                                 "tool_input": {"q": "how do I drop a table"}, "cwd": clean}, N),
        ("Edit tool", {"tool_name": "Edit", "tool_input": {"file_path": "x"}, "cwd": clean}, N),
        ("invalid json", "{nope", N),
        ("empty event", {}, N),
    ]

    bad = 0
    for case in cases:
        name, ev, want = case[:3]
        must = case[3] if len(case) > 3 else None
        got, why = run_guard(ev)
        ok = got == want and (must is None or must in why)
        bad += not ok
        print("%s  %-26s want=%-5s got=%-5s %s" % ("ok " if ok else "BAD", name, want, got,
                                                  "" if ok else why.replace("\n", " | ")[:160]))

    # CLI scans
    r = subprocess.run([sys.executable, "-I", GUARD, "--scan-worktree", worktree],
                       capture_output=True, text=True, env=ENV, stdin=subprocess.DEVNULL, timeout=60)
    ok = r.returncode == 1 and FAKE_KEY not in r.stdout
    bad += not ok
    print("%s  %-26s want=exit1, no secret echoed got=exit%d" % ("ok " if ok else "BAD", "scan-worktree hit", r.returncode))
    r = subprocess.run([sys.executable, "-I", GUARD, "--scan-unpushed", clean],
                       capture_output=True, text=True, env=ENV, stdin=subprocess.DEVNULL, timeout=60)
    ok = r.returncode == 0
    bad += not ok
    print("%s  %-26s want=exit0 got=exit%d" % ("ok " if ok else "BAD", "scan-unpushed clean", r.returncode))
    for label, mode, d, want in (("scan-worktree untracked", "--scan-worktree", untracked, 1),
                                 ("scan non-git dir", "--scan-worktree", nogit, 2),
                                 ("scan bad regex", "--scan-unpushed", badrx, 2)):
        r = subprocess.run([sys.executable, "-I", GUARD, mode, d], capture_output=True, text=True,
                           env=ENV, stdin=subprocess.DEVNULL, timeout=60)
        ok = r.returncode == want
        bad += not ok
        print("%s  %-26s want=exit%d got=exit%d" % ("ok " if ok else "BAD", label, want, r.returncode))
    for label, args in (("scan without DIR -> usage", ["--scan-worktree"]),
                        ("unknown option -> usage", ["--scan"])):
        r = subprocess.run([sys.executable, "-I", GUARD] + args, capture_output=True, text=True,
                           env=ENV, stdin=subprocess.DEVNULL, timeout=60)
        ok = r.returncode == 2
        bad += not ok
        print("%s  %-26s want=exit2 got=exit%d" % ("ok " if ok else "BAD", label, r.returncode))
    extra = 5
    _, why = run_guard(bash("git push", leaky))
    ok = FAKE_KEY not in why
    bad += not ok
    print("%s  %-26s secret not echoed in deny reason" % ("ok " if ok else "BAD", "deny reason"))

    subprocess.run(["rm", "-rf", tmp])
    total = len(cases) + 3 + extra
    print("selftest: %d/%d passed" % (total - bad, total))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
