---
name: verify
description: Run the devproc re-verify checklist (typecheck, tests, lint, build, git status, leak scan, failure-mode check) and save raw output to the task's run/ folder. Use before every presentation, commit request or status post.
---

Re-verify checklist. Config: `.claude/devproc/config.json` (`commands.typecheck`, `test`, `lint`, `build`).

1. Evidence folder: `.claude/devproc/tasks/<CURRENT>/run/verify-<YYYYMMDD-HHMMSS>/` (no CURRENT: `.claude/devproc/run/verify-<...>/`).
2. For each configured command that is not empty, run it with `set -o pipefail` (no `-e`, so every check runs), full output to `<name>.log` in that folder, and record the exit code from the command itself, never after `| tail`. An empty command: ask the user once what to run, then save it in `config.json`.
3. Lint only touched files where the linter allows it: `git diff --name-only HEAD` plus untracked files.
4. `git status --porcelain` to `git-status.txt`. Every changed or untracked path must be one you intended; list any surprise.
5. Leak scan: `python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/guard.py" --scan-worktree .` (uncommitted and untracked files) and `... --scan-unpushed .` (commits not on the upstream remote), output to `leak.txt`. Exit 0 = clean. Exit 1 = a hit: stop and fix before anything else. Exit 2 = the scan could NOT run (not a git repo, no commits, bad `leakPatterns` regex): report it as not run, never as clean.
6. Failure-mode check, if the change touches a backend call: show in the code or by a test that a backend error surfaces as an error, never as an empty success.
7. Deletions: for every symbol removed in `git diff HEAD`, state where its callers went.
8. If you added a test or check in this step, prove it can fail: break the guarded code, run it, see red, restore, and record `sha256sum` of the restored file.

Report as a table: check, command, exit code, one-line summary, log path. Any non-zero exit is red; do not present the step as done.
