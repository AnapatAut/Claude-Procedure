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

Requirements: `python3` 3.8 or later, `git`, and the Claude Code CLI. Tested on Linux with Claude Code 2.1.310 and git 2.43. macOS is untested; Windows is not supported.

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
6. **Who owns the source.** One person owns changes to the devproc folder and runs the tests after every change. A change reaches an installed directory only when the version in `plugin/.claude-plugin/plugin.json` is raised and that directory runs `install.sh --upgrade --apply`. Each directory moves on its own; an upgrade from a changed source under the same version is refused.

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
- **Install, upgrade and repair roll back** when a step fails or the run is interrupted: files are restored, new snapshots and created directories removed, and the plugin and marketplace entries undone or pointed back.
  - A failed undo is reported as `rollback INCOMPLETE` (exit 3).
- **One run at a time** per directory; a second `--apply` run is refused.
- **A killed run** (SIGKILL, power loss) cannot roll back; Ctrl-C, SIGTERM and SIGHUP (terminal closed, ssh dropped) do, and a run under `nohup` keeps going. A second signal during the rollback is ignored, and a running `claude` call is let finish first. After a kill, install refuses while devproc's leftovers are there (a plugin snapshot, a registered marketplace, enabled plugin entries) and prints the command that removes each. devproc's marked git exclude lines are taken back by the next install. Rule files left behind count as the project's own afterwards: delete them by hand if you do not want them.
- **Uninstall does not roll back.** It stops at a failed step and can be rerun to finish.
  - It keeps files the project changed and all task data, and lists every kept file.
  - It keeps the git exclude lines for kept files, so evidence is not committed by accident.
  - It removes this directory's own marketplace and snapshots. Other directories are not touched, also when this directory is a copy of another. Uninstalling an original does remove the marketplace that its never-repaired copies borrow: repair those copies first.

## Test evidence (2026-10-25, version 0.2.1)

| Test | Result |
|---|---|
| Guard self-test, `tests/selftest.py` | 91/91 cases pass |
| Same test against a do-nothing fake guard | fails, as required |
| Same test against the first, unpublished guard version (0.1.0) | 22 of the 84 cases of the time fail: each one is a gap the review found and 0.2.0 fixes |
| Installer end-to-end, `tests/installer-e2e.sh`: sandbox config, throwaway dirs, real sessions' debug logs | 331/331 checks pass (details below) |
| Same e2e test against 78 installer mutants, each with one safeguard switched off (`tests/installer-mutants.py`) | each is caught by the check written for it |
| Live logged-in session (0.2.1) in a throwaway directory, push of a commit with a fake AWS key | the hook ran from the directory's snapshot (`…/.claude/devproc/dist/0.2.1/plugin/scripts/guard.py`) and denied the push; nothing pushed; after uninstall the real `settings.json`, `installed_plugins.json` and `known_marketplaces.json` are byte-identical |
| Live interactive session (guard 0.1.0), plugin loaded, `/usr/bin/git push` (a spelling simple permission rules miss) | Claude Code stopped with "Hook PreToolUse:Bash requires confirmation … devproc (P17)"; declined; nothing pushed |
| Live `claude -p` session (guard 0.2.0), `bash -c 'git push origin HEAD:main'` with a fake AWS key in the commit | denied: "leak scan found 1 hit(s) (1 commit(s) not on remote origin scanned): … cfg.py:1 [aws-access-key-id]", key not printed; nothing pushed |
| Real user config after all tests | unchanged (hashes compared) |
| Fresh-context review of all docs against the code | 25 findings; all fixed or documented here |
| Eleven fresh-context reviews of the 0.2.1 installer | 72 defects found, the last review none; all fixed |

The 331 installer checks cover:
- a dry run writes nothing;
- install, then a second install, changes nothing the second time;
- status reports which files the project changed;
- upgrade keeps edited files;
- uninstall keeps task data and edited files, keeps their exclude lines, and lists them;
- a non-git directory works;
- an existing project file is never overwritten;
- a file identical to a template is kept on uninstall;
- it refuses `$HOME`, `/`, its own folder (also through a symlink), a missing directory, a repo subdirectory and a linked worktree, each with its message;
- a second run on a locked directory is refused; bad arguments exit 2; a broken manifest or a missing `claude` gives a clean message;
- a failed install rolls back fully, also while another directory has devproc installed, and also when the step that failed had done part of its work;
- a `settings.local.json` that existed before install is byte-identical after uninstall;
- a failed upgrade restores the files it replaced;
- a failed uninstall stops honestly and a rerun finishes it;
- removing devproc from one directory leaves another directory working;
- a source change reaches a directory only through `--upgrade`;
- each directory loads its own read-only snapshot, checked in a real session's debug log;
- an upgrade from a changed source under the same version is refused and changes nothing;
- two directories on different versions, and upgrading one leaves the other alone;
- going back to the kept previous version reuses its snapshot; older snapshots are pruned, and an upgrade to the same version prunes none;
- a stray file or symlink in the snapshot folder is removed on uninstall, and the link's target is left alone;
- a damaged, missing or disabled install, a tampered snapshot `marketplace.json`, a symlink added to the snapshot, git exclude lines removed, a moved or copied project, and a marketplace name taken by another folder: `--status` reports each, `--repair` fixes each, and repair refuses a source at another version or with other code;
- a failed repair rolls back to exactly the state before it, registry included;
- a moved directory with a newer source: `--repair` refuses and points to `--upgrade`, which upgrades it, gives it a marketplace named for its new place and removes the old folder's dead registration;
- an installed directory copied into another repo: repair and upgrade refuse, and uninstall leaves that repo's exclude file alone;
- a git exclude file without a newline at its end keeps its last pattern working, and is byte-identical after uninstall;
- a failed install in a directory with a `settings.local.json` (CRLF line ends) restores it byte for byte; so does install then uninstall;
- a manifest or `settings.local.json` of the wrong JSON type gives a clean message, never a traceback;
- uninstalling a copy, or a moved folder, that was never repaired: the original's exclude file, git status and install stay as they were, and the copy's own records (subdirectories too) are removed;
- dry runs of install, upgrade, repair and uninstall write nothing;
- a 0.2.0 install (made with the real 0.2.0 installer) is migrated by `--upgrade`, while another 0.2.0 directory keeps working; its subdirectory records are removed; a failed migration rolls back to the working 0.2.0 install; a migration is refused when the project had the plugin before devproc;
- this uninstaller removes a 0.2.0 install, keeping the shared marketplace while another 0.2.0 directory uses it;
- a closed terminal (SIGHUP to the whole process group) and Ctrl-C: the running `claude` call finishes, and a second signal during the rollback does not cut it short;
- a step fails and SIGHUP arrives during its undo: the rollback still completes;
- a hung `claude` call is stopped at the timeout with the processes it started, even when one of them left its process group and still holds the output;
- an interrupt while an upgrade tidies up keeps the upgrade and names the tidying step not done, and only that one;
- a closed output pipe (`| head`) stops neither install nor uninstall;
- git answering in another language or in git 1.x wording, and a `GIT_DIR` set by the caller: the target is judged by its own repository, and the other repository is untouched;
- the remove command printed for a leftover works for a path with a quote in it;
- install over a 0.2.0 marker joined to the project's last pattern splits it off;
- a `.claude` symlinked to another directory's install: uninstall refuses, status exits 4, and the other install is untouched;
- at the end, no devproc marketplace or plugin record is left in the registry.

## Cost

- Always-on context: the 4 rule files, about 40 lines.
- Skills load only when invoked.
- The hook adds about 0.02 s to each Bash command, and about 0.05 s to a push.

## Known limits

- Hooks and rules are best-effort, as the Claude Code docs say.
- **A moved or copied project folder:** until `--repair`, a moved folder runs without devproc (Claude Code reports `failed to load`), and a copy runs the original's snapshot. `--status` names the problem.
  - A session started in a moved folder before the repair leaves a record of the old plugin id there. Its marketplace folder is gone, so a failed repair cannot reinstall it; the repair removes it as a last tidying step instead.
- **Tested with Claude Code 2.1.310.** The post-check relies on `readFromFolder` in `claude plugin list --json`.
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
