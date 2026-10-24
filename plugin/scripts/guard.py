#!/usr/bin/env python3
"""devproc PreToolUse guard.

Reads one hook event (JSON) on stdin. Decides:
  - git push in any spelling        -> "ask" (owner's explicit go). For a local push
                                       it first scans the commits not yet on the
                                       target remote; a leak hit -> "deny".
                                       When the repo or remote cannot be pinned
                                       down (ssh, GIT_DIR, --git-dir, quoted text),
                                       the ask says the scan was NOT run.
  - DB write or migration command   -> "ask"
  - anything else                   -> no output (Claude Code's normal rules apply)

Never returns "allow". On an internal error it returns "ask" when the input
looks like a push or DB command, and stays silent otherwise, so a bug can never
block ordinary commands. stdlib only, no network, writes nothing.

CLI (used by skills and the self-test):
  guard.py                      hook mode (stdin = hook event JSON)
  guard.py --scan-worktree DIR  leak scan of uncommitted and untracked files
  guard.py --scan-unpushed DIR  leak scan of commits not on the upstream remote
  exit codes for scans: 0 clean, 1 hit, 2 scan could not run
"""
import fnmatch
import json
import os
import pwd
import re
import shlex
import subprocess
import sys

VERSION = "0.2.0"
MAX_DIFF_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_REPORT = 10
GIT_TIMEOUT = 20  # the hook timeout (hooks.json) must stay well above this

# ---------------------------------------------------------------- patterns

DEFAULT_LEAK_PATTERNS = {
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "aws-access-key-id": r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",
    "aws-secret-key": r"(?i)\baws_secret_access_key\b\s*[:=]\s*\S{20,}",
    "github-token": r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})",
    "slack-token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "anthropic-key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "openai-key": r"\bsk-(proj-|svcacct-)?[A-Za-z0-9_-]{32,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "assigned-secret": r"(?i)\b(api[_-]?key|secret|passw(or)?d|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
    "url-credentials": r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{3,}@",
}

SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "cockroach", "clickhouse-client", "sqlcmd", "duckdb"}
MONGO_CLIENTS = {"mongosh", "mongo"}
REDIS_CLIENTS = {"redis-cli"}
DB_ALWAYS_ASK = {"pg_restore", "createdb", "dropdb", "createuser", "dropuser"}
SQL_FLAGS = {"-c", "--command", "-e", "--execute", "--eval"}
FILE_FLAGS = {"-f", "--file"}
SQL_WRITE = re.compile(
    r"(?i)\b(insert\s+into|update\s+\S+\s+set|delete\s+from|drop|alter|create|truncate|grant|revoke|"
    r"merge\s+into|upsert|replace\s+into|copy\s+\S+\s+from|vacuum|reindex|cluster|refresh\s+materialized|"
    r"call\s+\w|do\s+\$|comment\s+on|lock\s+table)\b")
MONGO_WRITE = re.compile(r"(?i)\.(insert|update|delete|drop|remove|replace|bulkWrite|create|rename)\w*\s*\(")
REDIS_WRITE = re.compile(
    r"(?i)^\s*(set|setex|setnx|mset|del|unlink|flushall|flushdb|hset|hdel|lpush|rpush|lpop|rpop|sadd|srem|"
    r"zadd|zrem|expire|persist|rename|incr\w*|decr\w*|append|xadd|eval|config\s+set)\b")
MIGRATION = re.compile(
    r"\b(supabase\s+(db\s+(push|reset)|migration\s+(up|repair))"
    r"|prisma\s+(migrate\s+(deploy|dev|reset)|db\s+(push|execute|seed))"
    r"|drizzle-kit\s+(push|migrate)"
    r"|knex\s+(migrate|seed)"
    r"|alembic\s+(upgrade|downgrade|stamp)"
    r"|(rails|rake)\s+db:"
    r"|sequelize(-cli)?\s+db:"
    r"|typeorm\s+(migration:run|schema:sync)"
    r"|flyway\s+(migrate|clean|repair)"
    r"|liquibase\s+update"
    r"|atlas\s+(schema|migrate)\s+apply"
    r"|dbmate\s+(up|migrate|rollback|drop)"
    r"|goose\s+(\S+\s+)*(up|down|reset)\b"
    r"|(npm|pnpm|yarn|bun)\s+(run\s+)?\S*(migrat|db:push|db:reset|seed)\S*)")
# an MCP tool counts as a database tool when its server or tool name says so
MCP_DB_NAME = re.compile(r"(?i)(^|_)(sql|sqlite|postgres|postgresql|pg|supabase|mysql|mariadb|mongo|mongodb|"
                         r"redis|database|db|neon|planetscale|bigquery|snowflake|clickhouse|duckdb)(_|$)")

GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                     "--exec-path", "--super-prefix", "--config-env"}
GIT_BUILTINS = set("""add am apply archive bisect blame branch bundle cat-file checkout
cherry cherry-pick clean clone commit config describe diff difftool fetch for-each-ref
format-patch fsck gc grep help init log ls-files ls-remote ls-tree merge merge-base mv
notes pull range-diff rebase reflog remote reset restore rev-list rev-parse revert rm
shortlog show show-ref sparse-checkout stash status submodule switch symbolic-ref tag
update-index version worktree""".split())
PUSH_OPTS_WITH_ARG = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
REMOTE_RUNNERS = {"ssh", "tsh", "mosh", "docker", "podman", "kubectl", "lxc", "vagrant", "gcloud", "az"}
PUSH_RX = re.compile(r"\bgit\b.*\bpush\b", re.S)
QUICK = re.compile(r"push|\bgit\b|psql|mysql|mariadb|sqlite|mongo|redis|cockroach|clickhouse|sqlcmd|duckdb"
                   r"|pg_restore|createdb|dropdb|createuser|dropuser|migrat|db:|drizzle|prisma"
                   r"|supabase|alembic|flyway|liquibase|atlas|dbmate|goose|knex|typeorm|sequelize|seed")


# ---------------------------------------------------------------- shell parsing

def tokenize(text):
    """Tokens with shell punctuation kept. None when quoting is unbalanced."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def is_separator(tok):
    return bool(tok) and set(tok) <= set(";&|()")


def segments(text):
    """Split a command line into simple commands. Each item is a token list, or
    the marker "(" / ")" for a subshell boundary. None when quoting cannot be parsed.
    Lines are parsed one by one; if a quote spans lines, the text is one line."""
    groups = [tokenize(line) for line in text.split("\n")]
    if any(g is None for g in groups):
        whole = tokenize(text.replace("\n", " "))
        if whole is None:
            return None
        groups = [whole]
    out = []
    for toks in groups:
        cur = []
        for t in toks:
            if is_separator(t):
                if cur:
                    out.append(cur)
                cur = []
                for ch in t:
                    if ch in "()":
                        out.append(ch)
            else:
                cur.append(t)
        if cur:
            out.append(cur)
    return out


def base(tok):
    return os.path.basename(tok)


def git_subcommand(toks, i, cwd):
    """toks[i] is a git binary. Returns (subcommand, repo_dir, uses_git_dir, args after subcommand)."""
    j = i + 1
    repo, gitdir = cwd, False
    while j < len(toks):
        t = toks[j]
        if t in GIT_OPTS_WITH_ARG:
            if t == "-C" and j + 1 < len(toks):
                repo = os.path.normpath(os.path.join(repo, os.path.expanduser(toks[j + 1])))
            if t in ("--git-dir", "--work-tree"):
                gitdir = True
            j += 2
        elif t.startswith("--git-dir=") or t.startswith("--work-tree="):
            gitdir = True
            j += 1
        elif t.startswith("-"):
            j += 1
        else:
            return t, repo, gitdir, toks[j + 1:]
    return None, repo, gitdir, []


def git_alias_is_push(sub, repo):
    try:
        r = subprocess.run(["git", "-C", repo, "config", "--get", "alias." + sub],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return r.returncode == 0 and re.search(r"\bpush\b", r.stdout) is not None


def push_remote(args):
    """First non-option argument of `git push` (remote name or URL), or None."""
    k = 0
    while k < len(args):
        a = args[k]
        if a in PUSH_OPTS_WITH_ARG:
            k += 2
            continue
        if a.startswith("-"):
            k += 1
            continue
        return a
    return None


def shell_script(toks, i):
    """toks[i] is a shell; return the script of `-c SCRIPT`, or None."""
    for k in range(i + 1, len(toks) - 1):
        if re.fullmatch(r"-[a-z]*c[a-z]*", toks[k]):
            return toks[k + 1]
        if not toks[k].startswith("-"):
            return None
    return None


def find_pushes(text, cwd, depth=0, how="local"):
    """List of (repo_dir, remote, how). how:
       local  = runs here, repo known: scan it;
       remote = runs on another machine or container: no local scan;
       unsure = repo or command cannot be pinned down: no scan, say so."""
    if depth > 4:
        return [(cwd, None, "unsure")] if PUSH_RX.search(text) else []
    segs = segments(text)
    if segs is None:
        return [(cwd, None, "unsure")] if PUSH_RX.search(text) else []
    found = []
    for inner in re.findall(r"`([^`]*)`", text):
        found += find_pushes(inner, cwd, depth + 1, how)
    cur, stack = cwd, []
    for seg in segs:
        if seg == "(":
            stack.append(cur)
            continue
        if seg == ")":
            cur = stack.pop() if stack else cwd
            continue
        toks = seg
        lead = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if lead and lead[0] in ("cd", "pushd"):
            dest = lead[1] if len(lead) > 1 and not lead[1].startswith("-") else "~"
            cur = os.path.normpath(os.path.join(cur, os.path.expanduser(dest)))
            continue
        seg_how = "remote" if lead and base(lead[0]) in REMOTE_RUNNERS else how
        env_git = any(re.match(r"^GIT_(DIR|WORK_TREE)=", t) for t in toks)
        skip = set()
        for i, t in enumerate(toks):
            if i in skip:
                continue
            b = base(t)
            if b in ("git", "git.exe"):
                sub, repo, gitdir, rest = git_subcommand(toks, i, cur)
                if sub == "push" or (sub and sub not in GIT_BUILTINS and git_alias_is_push(sub, repo)):
                    h = seg_how
                    if h == "local" and (env_git or gitdir):
                        h = "unsure"
                    found.append((repo, push_remote(rest) if sub == "push" else None, h))
            elif b in SHELLS:
                script = shell_script(toks, i)
                if script is not None:
                    skip.add(toks.index(script, i))
                    found += find_pushes(script, cur, depth + 1, seg_how)
            elif t == "eval" and i + 1 < len(toks):
                found += find_pushes(" ".join(toks[i + 1:]), cur, depth + 1, seg_how)
                break
            elif (" " in t or "\t" in t or "\n" in t) and PUSH_RX.search(t):
                # quoted text: a remote command, or just words (commit message, echo)
                found += find_pushes(t, cur, depth + 1, "remote" if seg_how == "remote" else "unsure")
    return found


def read_file(path, cwd):
    p = os.path.join(cwd, os.path.expanduser(path))
    try:
        with open(p, "r", errors="replace") as f:
            return f.read(2 * 1024 * 1024)
    except OSError:
        return None


def find_db(text, cwd, depth=0):
    """List of reasons why this command looks like a DB write or migration."""
    reasons = []
    if depth > 4:
        return reasons
    m = MIGRATION.search(text)
    if m:
        reasons.append("migration command `%s`" % m.group(0).strip())
    segs = segments(text)
    if segs is None:
        return reasons
    for inner in re.findall(r"`([^`]*)`", text):
        reasons += find_db(inner, cwd, depth + 1)
    for toks in segs:
        if toks in ("(", ")"):
            continue
        for i, t in enumerate(toks):
            b = base(t)
            if b in DB_ALWAYS_ASK:
                reasons.append("`%s` changes a database" % b)
            elif b in SQL_CLIENTS or b in MONGO_CLIENTS or b in REDIS_CLIENTS:
                reasons += db_client(b, toks[i + 1:], text, cwd)
            elif " " in t and QUICK.search(t):
                reasons += find_db(t, cwd, depth + 1)
    return reasons


def db_client(b, rest, text, cwd):
    stmts, unknown = [], False
    k = 0
    while k < len(rest):
        a = rest[k]
        if a in SQL_FLAGS and k + 1 < len(rest):
            stmts.append(rest[k + 1]); k += 2; continue
        if any(a.startswith(f + "=") for f in SQL_FLAGS if f.startswith("--")):
            stmts.append(a.split("=", 1)[1]); k += 1; continue
        if (a in FILE_FLAGS or a == "<") and k + 1 < len(rest):
            body = read_file(rest[k + 1], cwd)
            if body is None:
                unknown = True
            else:
                stmts.append(body)
            k += 2; continue
        k += 1
    pos = [a for a in rest if not a.startswith("-")]
    if b == "sqlite3" and len(pos) >= 2:
        stmts.append(" ".join(pos[1:]))
    if b in REDIS_CLIENTS and pos and not stmts:
        stmts.append(" ".join(pos))
    if "<<" in text:
        stmts.append(text)
    joined = "\n".join(stmts)
    if unknown:
        return ["`%s` runs a file devproc cannot read" % b]
    if not joined.strip():
        return ["`%s` with no visible statement (interactive or piped)" % b]
    rx = MONGO_WRITE if b in MONGO_CLIENTS else REDIS_WRITE if b in REDIS_CLIENTS else SQL_WRITE
    hit = rx.search(joined)
    if hit:
        return ["`%s` statement contains `%s`" % (b, hit.group(0).strip().split("(")[0].upper())]
    return []


# ---------------------------------------------------------------- leak scan

def load_config(*dirs):
    for d in dirs:
        if not d:
            continue
        p = os.path.join(d, ".claude", "devproc", "config.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                if not isinstance(cfg, dict):
                    return {"_error": "%s: top level must be a JSON object" % p}, p
                return cfg, p
            except (OSError, ValueError) as e:
                return {"_error": "%s: %s" % (p, e)}, p
    return {}, None


def current_user():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER") or ""


def patterns(cfg):
    """Compiled patterns. Raises ValueError naming a bad project pattern."""
    pats = dict(DEFAULT_LEAK_PATTERNS)
    user = current_user()
    if user:
        homes = {"/home/" + user, "/Users/" + user}
        home = os.path.expanduser("~")
        if os.path.basename(home) == user and home != "/root":
            homes.add(home)
        pats["home-path"] = r"(?<![\w.-])(?:%s)\b" % "|".join(re.escape(h) for h in sorted(homes))
    extra = cfg.get("leakPatterns") or {}
    if not isinstance(extra, dict):
        raise ValueError("leakPatterns must be a JSON object of name: regex")
    pats.update(extra)
    out = {}
    for name, rx in pats.items():
        try:
            out[name] = re.compile(rx)
        except (re.error, TypeError) as e:
            raise ValueError("leakPatterns entry %r is not a valid regex (%s)" % (name, e))
    return out


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, timeout=GIT_TIMEOUT)


def toplevel(repo):
    r = git(repo, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return r.stdout.decode(errors="replace").strip()


def scan_patch(patch, pats, ignore):
    """Scan added lines of a unified or combined (--cc) diff.
    Returns list of (commit, file, line, name)."""
    hits = []
    commit, path, line = "", "", 0
    for raw in patch.split("\n"):
        if raw.startswith("\x00"):
            commit = raw[1:13]
            continue
        if raw.startswith("+++ "):
            p = raw[4:]
            path = p[2:] if p.startswith("b/") else p
            continue
        if raw.startswith("@@"):
            m = re.search(r" \+(\d+)", raw)
            line = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            text = raw[1:]
            if text.startswith("+"):  # combined diff: added against every parent
                text = text[1:]
            if path and not any(fnmatch.fnmatch(path, g) for g in ignore):
                for name, rx in pats.items():
                    if rx.search(text):
                        hits.append((commit, path, line, name))
            line += 1
    return hits


def untracked_patch(top):
    """Untracked, not-ignored files as a synthetic all-added patch."""
    r = git(top, "ls-files", "--others", "--exclude-standard", "-z")
    out = []
    for rel in r.stdout.decode(errors="replace").split("\0"):
        if not rel:
            continue
        p = os.path.join(top, rel)
        try:
            if os.path.getsize(p) > MAX_FILE_BYTES:
                continue
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        out.append("+++ b/%s\n@@ -0,0 +1 @@" % rel)
        out += ["+" + l for l in data.decode(errors="replace").split("\n")]
    return "\n".join(out)


def scan_base(top, remote):
    """Refs that count as 'already pushed' for this push, and a label."""
    if remote is None:
        r = git(top, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        up = r.stdout.decode(errors="replace").strip()
        remote = up.split("/", 1)[0] if r.returncode == 0 and "/" in up else "origin"
    names = git(top, "remote").stdout.decode(errors="replace").split()
    if remote in names:
        refs = git(top, "for-each-ref", "--format=%(refname)", "refs/remotes/%s/" % remote).stdout.strip()
        if refs:
            return ["--not", "--remotes=%s" % remote], "remote %s" % remote
        return [], "remote %s (no tracking refs: whole history)" % remote
    return [], "push target %s (not a named remote: whole history)" % remote


def leak_scan(repo, mode, remote=None):
    """mode 'unpushed' or 'worktree'. Returns (hits, note); hits None = could not run."""
    try:
        top = toplevel(repo)
        if not top:
            return None, "not a git repository: %s" % repo
        cfg, cfg_path = load_config(top, os.environ.get("CLAUDE_PROJECT_DIR"))
        if "_error" in cfg:
            return None, "config unreadable (%s)" % cfg["_error"]
        try:
            pats = patterns(cfg)
        except ValueError as e:
            return None, str(e)
        ignore = cfg.get("leakIgnorePaths") or []
        if not isinstance(ignore, list) or not all(isinstance(g, str) for g in ignore):
            return None, "leakIgnorePaths must be a JSON list of glob strings"
        if mode == "unpushed":
            excl, label = scan_base(top, remote)
            r = git(top, "log", "-p", "--cc", "--no-color", "--no-ext-diff", "--unified=0",
                    "--format=%x00%H", "HEAD", "--branches", "--tags", *excl)
            if r.returncode != 0:
                return None, "git failed: %s" % r.stderr.decode(errors="replace").strip()[:200]
            if len(r.stdout) > MAX_DIFF_BYTES:
                return None, "history to scan is larger than %d MB" % (MAX_DIFF_BYTES // 2**20)
            patch = r.stdout.decode(errors="replace")
            n = patch.count("\x00")
            note = ("%d commit(s) not on %s scanned" % (n, label)) if n else ("no commits that are not already on %s" % label)
        else:
            r = git(top, "diff", "HEAD", "--no-color", "--no-ext-diff", "--unified=0")
            if r.returncode != 0:  # no commits yet
                r = git(top, "diff", "--cached", "--no-color", "--no-ext-diff", "--unified=0")
            if r.returncode != 0:
                return None, "git failed: %s" % r.stderr.decode(errors="replace").strip()[:200]
            patch = r.stdout.decode(errors="replace") + "\n" + untracked_patch(top)
            note = "uncommitted and untracked files scanned"
        hits = scan_patch(patch, pats, ignore)
        if cfg_path:
            note += ", config %s" % os.path.relpath(cfg_path, top)
        return hits, note
    except subprocess.TimeoutExpired:
        return None, "git timed out after %d s" % GIT_TIMEOUT


def fmt_hits(hits):
    lines = ["%s %s:%d [%s]" % (c or "worktree", p, l, n) for c, p, l, n in hits[:MAX_REPORT]]
    if len(hits) > MAX_REPORT:
        lines.append("... %d more" % (len(hits) - MAX_REPORT))
    return "\n".join(lines)


# ---------------------------------------------------------------- hook

def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


def decide(event):
    """Return (decision, reason) or None."""
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    if tool.startswith("mcp__"):
        parts = tool.split("__")
        if not any(MCP_DB_NAME.search(p) for p in parts[1:]):
            return None
        blob = json.dumps(tin)
        hit = SQL_WRITE.search(blob) or MONGO_WRITE.search(blob)
        if hit:
            return ("ask", "devproc (P18): `%s` input contains `%s`. Shared databases are "
                    "read-only by default; a write needs a written, scoped approval."
                    % (tool, hit.group(0).strip().split("(")[0].upper()))
        return None

    if tool != "Bash":
        return None
    cmd = tin.get("command") or ""
    if not QUICK.search(cmd):
        return None

    deny, ask = [], []
    for repo, remote, how in find_pushes(cmd, cwd):
        if how == "remote":
            ask.append("push runs on another machine or container: leak scan NOT run")
            continue
        if how == "unsure":
            ask.append("push spelling devproc cannot pin to a repo (GIT_DIR, --git-dir, or quoted text): "
                       "leak scan NOT run")
            continue
        hits, note = leak_scan(repo, "unpushed", remote)
        if hits is None:
            ask.append("leak scan could NOT run (%s)" % note)
        elif hits:
            deny.append("leak scan found %d hit(s) (%s):\n%s\n"
                        "Remove them from the commits, or add a narrow leakIgnorePaths entry "
                        "with the owner's OK." % (len(hits), note, fmt_hits(hits)))
        else:
            ask.append("leak scan clean: %s" % note)

    db = find_db(cmd, cwd)

    if deny:
        return ("deny", "devproc (P8): push blocked.\n" + "\n".join(deny))
    parts = []
    if ask:
        parts.append("devproc (P17): git push needs the owner's explicit go, every time. "
                     + "; ".join(ask) + ".")
    if db:
        parts.append("devproc (P18): looks like a DB write or migration: " + "; ".join(sorted(set(db)))
                     + ". Shared databases are read-only by default; a write needs a written, "
                     "scoped approval with a pre-image and a verify step.")
    if parts:
        return ("ask", "\n".join(parts))
    return None


def hook_main():
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        d = decide(event)
    except Exception as e:  # never block ordinary work on a guard bug
        cmd = json.dumps(event.get("tool_input") or {})
        if QUICK.search(cmd):
            emit("ask", "devproc guard error (%s: %s); asking to be safe." % (type(e).__name__, e))
        return 0
    if d:
        emit(*d)
    return 0


def cli_scan(mode, repo):
    hits, note = leak_scan(os.path.abspath(repo), mode)
    if hits is None:
        print("devproc leak scan: could NOT run: %s" % note)
        return 2
    if hits:
        print("devproc leak scan: %d hit(s), %s\n%s" % (len(hits), note, fmt_hits(hits)))
        return 1
    print("devproc leak scan: clean, %s" % note)
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--version"]:
        print(VERSION); sys.exit(0)
    if len(a) == 2 and a[0] in ("--scan-worktree", "--scan-unpushed"):
        sys.exit(cli_scan(a[0][7:], a[1]))
    if a:
        sys.stderr.write("usage: guard.py [--version | --scan-worktree DIR | --scan-unpushed DIR]\n"
                         "  with no arguments it runs as the hook: one event (JSON) on stdin\n")
        sys.exit(2)
    sys.exit(hook_main())
