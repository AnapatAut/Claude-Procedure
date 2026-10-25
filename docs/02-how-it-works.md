# 2. How it works

## A task under devproc

1. `/devproc:start fix-login-redirect Light TICKET-123`
   - Creates `.claude/devproc/tasks/2026-10-24-fix-login-redirect/` with `TASK.md`, `DECISIONS.md` and `run/`.
   - Records the profile (Light, Standard or Heavy) and the mode (step-gated by default).
2. `/devproc:baseline capture login -- curl -s localhost:3000/login -i`
   - Saves the output before any edit, with the git SHA and the exact command.
   - A baseline is never replaced afterwards.
3. Claude makes the change.
4. `/devproc:baseline compare login`
   - Diffs the new output against the baseline.
   - Every difference must be listed under "Expected changes" in `DECISIONS.md`. Anything else counts as a regression.
5. `/devproc:verify`
   - Runs typecheck, tests, lint and build from `config.json`, plus `git status`, a leak scan and a failure-mode check.
   - Raw output goes to `run/verify-<time>/`.
   - Any non-zero exit is red.
6. `/devproc:present`
   - Shows the step in a fixed format: diff, check table, mistakes made, SAFE / CORRECT / SECURE, manual test steps, decisions needed, out-of-scope finds.
   - Then Claude stops and waits for you.
7. You say "commit". Later you say "push":
   - The hook runs a leak scan on the commits that are not yet on the target remote.
   - If the scan finds a secret, the push is blocked, and the message names the file, line and pattern (never the secret itself).
   - If the scan is clean, Claude Code asks you to confirm the push.
8. `/devproc:close`
   - Drafts the tracker comment, with every number taken from a command.
   - It is posted only on your go. Done is never set by Claude.

Two more skills cover the rest of the procedure:
- `/devproc:decision` records your rulings, quoted and dated.
- `/devproc:mistake` logs mistakes and applies the recurrence ladder:
  - first repeat of a rule: automate it or add a checklist line;
  - second repeat: the step cannot close until the owner acknowledges it in writing.

## Files in a project

```
<project>/
  .claude/
    rules/devproc/        00-precedence.md  10-modes.md  20-safety.md  30-work.md
    devproc/
      config.json         commands, leak patterns, ignored paths
      MISTAKES.md         rules, ledger, ladder
      manifest.json       what devproc installed, with hashes (used by status, upgrade, repair, uninstall)
      dist/<version>/     read-only plugin snapshot: what Claude Code loads in this directory
      CURRENT             name of the active task folder
      tasks/<date>-<slug>/ TASK.md  DECISIONS.md  run/
    settings.local.json   registers the snapshot as this directory's marketplace and enables the plugin, for this directory only
```

In a git repo, devproc must be installed at the top level of the main checkout, and these paths go into `.git/info/exclude`, which is local and never committed. Nothing is committed by accident, also after uninstall: the exclude lines stay while task data stays.

To share the rules with a team:
- in `.git/info/exclude`, delete the `/.claude/rules/devproc/` line and the `# devproc: …` line above it, then commit that folder;
- add the config with `git add -f .claude/devproc/config.json`. Its `leakPatterns` then become readable by everyone with the repo: in a public repo, keep internal host names out of it;
- keep the `/.claude/devproc/` line: that folder also holds `manifest.json` (with the absolute path of the devproc source), the plugin snapshot, `CURRENT` and task evidence.

After that:
- an uninstall deletes the committed rule files and `config.json` that are still as seeded, and git shows them as deleted;
- a teammate who runs `install.sh` gets the exclude line added again in their clone. Tracked files stay tracked, but a new rule file they create is hidden from `git status` until they add it with `git add -f`;
- committed rule files identical to the templates count as the project's own in a teammate's install, so upgrades never change them. Update them by hand and commit.

## The rules (always loaded)

| File | Says |
|---|---|
| `00-precedence.md` | which document wins on a conflict; facts come from commands, not memory; evidence lives in `run/`; a check must be proven able to fail |
| `10-modes.md` | step-gated by default; autonomous runs only under a written mandate; the 7-item stop list; the presentation format |
| `20-safety.md` | push only on a go; staged paths explicit; DB read-only by default; protection only grows (no deleting tests to go green) |
| `30-work.md` | baseline before the edit; differences need a DECISIONS row; the re-verify list; the mistake ladder; how decisions are recorded |

The project owns these files. Edit them as its procedure evolves. Upgrades never overwrite a file the project has changed.

## The guard hook

It runs before every Bash command and every MCP tool call. It takes about 0.02 s, or about 0.05 s for a push. It writes nothing and never auto-approves anything. Its messages name the procedure part they enforce: P8, P17 or P18 in `docs/PROCEDURE.md`.

| Command | Decision |
|---|---|
| a local push: `git push`, `git -C x push`, `/usr/bin/git push`, `env X=1 git push`, `cd x && git push`, `(cd x; git push)`, `bash -c 'git push'`, `$(git push)`, `` `git push` ``, `eval git push`, alias `git p` = push | **ask**, after a leak scan of the commits not yet on the target remote (the reason says what was scanned) |
| any local push above, when those commits contain a secret pattern (also in a merge commit) | **deny** |
| `ssh box 'git push'`, `GIT_DIR=… git push`, `git --git-dir=… push`, quoted text such as a commit message mentioning git push | **ask**, and the reason says the leak scan was NOT run |
| `psql -c 'DROP TABLE x'`, `psql -f migrate.sql` (file contains a write), `psql` with no visible statement, `sqlite3 db 'delete …'`, `dropdb`, `supabase db push`, `prisma migrate deploy`, `npm run db:push` | **ask** |
| `redis-cli set k v`, `mongosh --eval 'db.x.insertOne(…)'` | **ask** |
| an MCP tool whose server or tool name says database (`db`, `sql`, `postgres`, `supabase`, `mongo`, …) and whose input contains a write | **ask** |
| `git status`, `git log --grep push`, `grep push`, `psql -c 'select 1'`, `psql -c "select replace(…)"`, `redis-cli get k`, everything else | no effect |

Built-in leak patterns:
- private keys;
- AWS key ids (`AKIA…`, `ASIA…`) and `aws_secret_access_key = …`;
- GitHub tokens (`ghp_…` and similar, `github_pat_…`), Slack, Anthropic and OpenAI keys;
- JWTs;
- `password = "…"`-style assignments;
- credentials inside URLs;
- your home path: `/home/<you>`, `/Users/<you>`, and `$HOME` when it ends in your user name (except `/root`).

Each project adds its own patterns (hostnames, ports, key names) in `config.json` under `leakPatterns`; the README, "Adapting it", gives the format. `leakIgnorePaths` excludes fixture folders. An invalid pattern makes the scan report "could NOT run", never "clean".

`/devproc:verify` also scans uncommitted and untracked files. Scans need git: in a directory that is not a git repo they report "could NOT run".

## The reviewer agent

`devproc-reviewer` has the Read, Grep, Glob and Bash tools and none of the session's context. It is read-only by instruction: Bash could write, so its prompt forbids edits, commits, pushes and DB writes. It reads the rules, the task files, the diff and `run/`, then lists findings with a severity: blocker, should-fix or note. Use it before presenting a Standard or Heavy step.
