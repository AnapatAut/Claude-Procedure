# 3. Adopting it

## Try it in 10 minutes, on a throwaway repo

Run this from the devproc clone, as your own user. Each user's Claude config and projects stay their own.

```bash
DEVPROC="$PWD"
T="$(mktemp -d)/try"
mkdir -p "$T" && git -C "$T" init -q && git init -q --bare "$T.remote.git"
git -C "$T" remote add origin "$T.remote.git"
echo hello > "$T/a.txt" && git -C "$T" add a.txt && git -C "$T" -c user.name=demo -c user.email=demo@example.invalid commit -qm first
"$DEVPROC/install.sh" "$T"            # dry run: prints the plan, writes nothing
"$DEVPROC/install.sh" --apply "$T"    # install
(cd "$T" && claude)                   # try /devproc:start, then ask Claude to run: git push -u origin HEAD
"$DEVPROC/uninstall.sh" --apply "$T"  # remove
rm -rf "$(dirname "$T")"
```

The push goes to a local bare repo, so nothing leaves the machine. The guard scans the commit and asks you first.

Requirements: `python3` 3.8 or later, `git`, and the Claude Code CLI. Tested on Linux with Claude Code 2.1.306 and git 2.43. macOS is untested; Windows is not supported.

## Where it can be installed

- Any directory you own, except `/`, your home directory and the devproc folder.
- Inside a git repo: only the **top level of the main checkout**. Claude Code keeps local plugin settings there, so a subdirectory or a linked worktree is refused, and the message names the right directory. Example: for a worktree at `../app-feature` of the repo `app`, install in `app`.
- A directory that is not a git repo works, but leak scans need git there and report "could NOT run".

## Decisions for a team adopting it

1. **Which repos.** devproc is per directory, so it can start with one repo.
2. **Commit the rules, or keep them local.** By default everything stays local. Committing `.claude/rules/devproc/` and `config.json` gives everyone on the repo the same rules; `02-how-it-works.md`, "Files in a project", says how without committing task evidence. The plugin itself is enabled per person (`settings.local.json`).
3. **Leak patterns.** Add the team's internal hostnames, ports and key names to `config.json` under `leakPatterns` (format: README, "Adapting it").
4. **Verify commands.** Fill in `commands` in `config.json`: typecheck, test (the runner CI uses), lint, build.
5. **Database access.** Give Claude sessions a read-only DB role. The hook is only a backstop.
6. **Who owns the source.** One person owns changes to the devproc folder and runs the tests after every change. A change reaches installed directories only when the version in `plugin/.claude-plugin/plugin.json` is raised and each directory runs `install.sh --upgrade --apply`.

## Safety of the installer

- **What it writes:**
  - inside `<project>/.claude/`;
  - to the repo's local `.git/info/exclude`;
  - to Claude Code's plugin registry and cache, through `claude plugin`.

  It never edits your Claude settings file, needs no sudo, and devproc makes no network calls itself.
- **Dry run unless `--apply` is given.** Running install twice changes nothing.
- **Never replaces a file it did not write**, or one changed since install. A file already identical to a template counts as the project's own. In `settings.local.json` and the exclude file it only adds and removes its own entries.
- **Before writing anything:**
  - the guard self-test must pass, and must fail on a do-nothing fake guard;
  - `claude plugin validate` must pass.
- **Install and upgrade roll back** when a step fails: files are restored, created directories removed, and the plugin and marketplace entries undone.
  - The `claude plugin update` step of an upgrade is not undone; rerun the upgrade.
  - A failed undo is reported as `rollback INCOMPLETE`.
- **Uninstall does not roll back.** It stops at a failed step and can be rerun to finish.
  - It keeps files the project changed and all task data, and lists every kept file.
  - It keeps the git exclude lines for kept files, so evidence is not committed by accident.
  - It removes the marketplace from Claude Code's registry only when no other directory uses it.

## Test evidence (2026-10-24)

| Test | Result |
|---|---|
| Guard self-test, `tests/selftest.py` | 91/91 cases pass |
| Same test against a do-nothing fake guard | fails, as required |
| Same test against the first, unpublished guard version (0.1.0) | 22 of the 84 cases of the time fail: each one is a gap the review found and 0.2.0 fixes |
| Installer end-to-end, `tests/installer-e2e.sh`: sandbox config, throwaway dirs | 70/70 checks pass (details below) |
| Same e2e test with the uninstaller deliberately broken | checks go red (proves it can fail) |
| Live interactive session (guard 0.1.0), plugin loaded, `/usr/bin/git push` (a spelling simple permission rules miss) | Claude Code stopped with "Hook PreToolUse:Bash requires confirmation … devproc (P17)"; declined; nothing pushed |
| Live `claude -p` session (guard 0.2.0), `bash -c 'git push origin HEAD:main'` with a fake AWS key in the commit | denied: "leak scan found 1 hit(s) (1 commit(s) not on remote origin scanned): … cfg.py:1 [aws-access-key-id]", key not printed; nothing pushed |
| Real user config after all tests | unchanged (hashes compared) |
| Two fresh-context reviews of all files against the code, before publishing | every finding fixed or listed under Known limits |

The 70 installer checks cover:
- a dry run writes nothing;
- install, then a second install, changes nothing the second time;
- status reports which files the project changed;
- upgrade keeps edited files;
- uninstall keeps task data and edited files, keeps their exclude lines, and lists them;
- a non-git directory works;
- an existing project file is never overwritten;
- a file identical to a template is kept on uninstall;
- it refuses `$HOME`, `/`, its own folder, a missing directory, a repo subdirectory and a linked worktree;
- a failed install rolls back fully, also while another directory has devproc installed;
- a failed upgrade restores the files it replaced;
- a failed uninstall stops honestly and a rerun finishes it;
- removing devproc from one directory leaves another directory working;
- a source change reaches a directory only through `--upgrade`.

## Cost

- Always-on context: the 4 rule files, about 40 lines.
- Skills load only when invoked.
- The hook adds about 0.02 s to each Bash command, and about 0.05 s to a push.

## Known limits

- Hooks and rules are best-effort, as the Claude Code docs say.
- **Platforms:** tested on Linux only.
- **Pushes it cannot see:** a push inside a script file, a `gh` command that pushes, or a shell alias or function. Git aliases are seen.
- **Pushes it sees but cannot scan:** ssh, `GIT_DIR`, `--git-dir`. It asks and says the scan was not run.
- **What counts as "not yet pushed":** commits not on the target remote's tracking refs.
  - A remote never fetched, or a URL target, means the whole history is scanned.
  - A secret on another local branch also blocks the push. This is on purpose.
- **False asks:** a commit message that mentions "git push" triggers an ask. This is on purpose: the guard errs toward asking.
- **Headless `claude -p`:** there is no one to ask, so an "ask" becomes a refusal.
- **DB guard:** it reads `-c`, `-f`, `<` and heredocs, and does not see SQL built at run time. MCP tools count as database tools by name.
- **Hook timeout:** 150 s. If Claude Code kills the hook, the command falls back to normal permission rules.

## Remove it

```bash
./uninstall.sh /path/to/project           # dry run
./uninstall.sh --apply /path/to/project
```
