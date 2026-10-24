# devproc

A general dev procedure (`docs/PROCEDURE.md`) packaged as Claude Code config, installed **per project directory**. Each project gets its own copy of the rules and changes them as it evolves. Nothing applies to directories where devproc is not installed.

- Docs for deciding whether to adopt it: `docs/01-why-and-what.md`, `docs/02-how-it-works.md`, `docs/03-adopting.md`.
- Design and research: `DESIGN.md`.

Requirements: `python3` 3.8 or later, `git`, and the Claude Code CLI. Tested on Linux with Claude Code 2.1.306 and git 2.43. macOS is untested; Windows is not supported.

## What a project gets

| Piece | Where | What it does |
|---|---|---|
| Rules (4 files, about 40 lines) | `<dir>/.claude/rules/devproc/` | always-on procedure: precedence, modes and stop list, git and DB safety, baseline / verify / mistakes / decisions. Project-owned: edit freely. |
| Config | `<dir>/.claude/devproc/config.json` | typecheck / test / lint / build commands, extra leak patterns, ignored paths |
| Mistakes ledger | `<dir>/.claude/devproc/MISTAKES.md` | rules, ledger, recurrence ladder |
| Plugin `devproc@devproc` (local scope) | enabled in `<dir>/.claude/settings.local.json` | hook + skills + reviewer agent, for this directory only |
| Hook (PreToolUse) | plugin | see below |
| Skills | plugin | `/devproc:start`, `baseline`, `verify`, `decision`, `mistake`, `present`, `close` |
| Agent | plugin | `devproc-reviewer`: fresh-context review, read-only by instruction (it has Bash) |

What the hook does:
- **Ask** on every `git push` spelling it recognises.
- Before a **local** push, it scans the commits not yet on the target remote; a leak-pattern hit means **deny**.
- If it cannot pin the push to a local repo (ssh, `GIT_DIR`, `--git-dir`, quoted text), it **asks** and says the scan was **not** run.
- **Ask** on DB writes and migration commands.

Task folders are created by `/devproc:start` under `<dir>/.claude/devproc/tasks/<date>-<slug>/` (`TASK.md`, `DECISIONS.md`, `run/`).

## Install, upgrade, remove

```bash
git clone https://github.com/AnapatAut/Claude-Procedure.git devproc && cd devproc
```

Run the commands below from that folder. Every command is a dry run unless `--apply` is given. Each user runs it as themselves.

```bash
./install.sh <dir>                    # show the plan
./install.sh --apply <dir>            # install
./install.sh --status <dir>           # state of each file (as seeded / evolved / missing / adopted)
./install.sh --upgrade --apply <dir>  # update files still as seeded + the plugin; keeps evolved files
./uninstall.sh --apply <dir>          # remove what devproc put there and the project did not change
```

- `<dir>` in a git repo must be the **top level of the main checkout**: Claude Code keeps local plugin settings there. A subdirectory or a linked worktree is refused, and the message names the right directory.
- Restart Claude Code sessions in `<dir>` after install or upgrade.
- An installed directory runs a **copy** of the plugin from Claude Code's cache (`~/.claude/plugins/cache/devproc/devproc/<version>/`), not this folder. A change to the source reaches a directory only after the `version` in `plugin/.claude-plugin/plugin.json` is raised and `install.sh --upgrade --apply <dir>` is run.
- **Install only with `install.sh`.** Do not add this repo with `/plugin marketplace add`: that gives the hook and skills without the rules and config, and possibly at user scope, so in every project.
- Keep the clone where it is while directories use it: the plugin registry and each manifest record its path. To move it, uninstall from those directories, move it, then install again.

## Adapting it

- **Per project, after install:** the project owns `.claude/rules/devproc/*.md`, `.claude/devproc/config.json` and `.claude/devproc/MISTAKES.md`. Edit them as the project's procedure evolves. Upgrades never overwrite a changed file.
- **`config.json`** example:

  ```json
  {
    "commands": { "typecheck": "npx tsc --noEmit", "test": "npm test", "lint": "npm run lint", "build": "npm run build" },
    "leakPatterns": { "internal-host": "\\bbuild-[0-9]+\\.corp\\b" },
    "leakIgnorePaths": ["fixtures/*"]
  }
  ```

  - `commands`: what `/devproc:verify` runs. An empty string means not set yet: verify asks once what to run and saves it.
  - `leakPatterns`: an object of `name: Python regex`. Double each backslash in JSON. A name equal to a built-in one replaces it: `private-key`, `aws-access-key-id`, `aws-secret-key`, `github-token`, `slack-token`, `anthropic-key`, `openai-key`, `jwt`, `assigned-secret`, `url-credentials`, `home-path`. To turn a built-in off, set it to `"(?!)"`. An invalid regex, or a list instead of an object, makes scans report "could NOT run".
  - `leakIgnorePaths`: a list of `fnmatch` globs on repo-relative paths; `*` also matches `/`. Anything other than a list of strings makes scans report "could NOT run".
- **Sharing the rules with a team:** see `docs/02-how-it-works.md`, "Files in a project". Never commit `.claude/devproc/` as a whole.
- **Changing the base (a fork):**
  - seed files are in `plugin/templates/`; an upgrade applies them only to directory copies still as seeded;
  - skills, agent and hook are in `plugin/skills/`, `plugin/agents/`, `plugin/scripts/guard.py`;
  - after a change, run the tests, raise `version` in `plugin/.claude-plugin/plugin.json`, then run `install.sh --upgrade --apply <dir>` in each directory.
- **Renaming:** the plugin name `devproc` is fixed in `lib/devproc_install.py`, `.claude-plugin/marketplace.json`, `plugin/.claude-plugin/plugin.json`, `tests/installer-e2e.sh`, the docs, and every `/devproc:` reference in the rules and skills. Change them all together. The folder names `.claude/devproc/` and `.claude/rules/devproc/` are separate and can stay.

## Safety

- Writes only inside `<dir>/.claude/`, the repo's local `.git/info/exclude` (never committed), and Claude Code's plugin registry through the `claude plugin` CLI. Never edits `~/.claude/settings.json`. No sudo. devproc itself makes no network calls.
- **Never replaces a file it did not write**, or one changed since it wrote it. A file already identical to a template is recorded as the project's own and is never updated or removed. In `settings.local.json` and the exclude file it only adds and removes its own entries. A second install changes nothing.
- **Preflight, before writing anything:**
  - the guard self-test passes, and fails on a do-nothing mutant guard;
  - `claude plugin validate` passes for the plugin and the marketplace.
- **Install and upgrade roll back** every completed step when a step fails. That includes restoring files an upgrade replaced and removing the directories install created.
  - Exception: the `claude plugin update` step of an upgrade is not undone; rerun the upgrade.
  - A failed undo is reported as `rollback INCOMPLETE`.
- **Uninstall does not roll back.** On a failure it stops, says how many steps were done, and can be rerun to finish.
- **Uninstall removes only:**
  - seeded files unchanged since install;
  - devproc's marketplace entry for this directory (the shared registry entry stays while other directories use it);
  - the exclude lines for paths left empty;
  - `settings.local.json` if devproc created it and it is now empty;
  - directories devproc created and left empty.

  It lists every file it keeps, and keeps the exclude lines for kept task data so evidence is not committed by accident.
- The hook script is stdlib Python. It reads the event on stdin and git state, writes nothing, and never answers "allow". On an internal error it asks (if the command looks like a push or DB command) or stays silent.
- Keep the devproc folder writable only by you: installs and upgrades copy the plugin from it.

## Limits

- Hooks and rules are best-effort, as the Claude Code docs say.
- **Pushes it cannot see:** a push from inside a script file, a `gh` command that pushes, or a shell alias or function that wraps `git push`. Git aliases (`git p` = push) are seen.
- **Leak scan range:** it covers commits not on the target remote's tracking refs. A remote with no tracking refs, or a URL target, means the whole history is scanned.
  - A secret on another local branch also blocks the push. This is on purpose.
  - Above 20 MB of diff the scan does not run, and the hook asks.
- **DB guard:** it is a backstop; only a read-only database role truly protects a database. It reads `-c`, `-f`, `<` and heredocs, and does not see SQL built at run time. An MCP tool counts as a database tool by its server or tool name (`db`, `sql`, `postgres`, `supabase`, …).
- **False asks:** a commit message or quoted text that mentions "git push" triggers an ask that says the scan was not run. This is on purpose.
- **Headless `claude -p`:** there is no one to ask, so an "ask" becomes a refusal.
- **Hook timeout:** 150 s. If Claude Code kills the hook, the command falls back to normal permission rules.

## Tests

```bash
python3 -I tests/selftest.py plugin/scripts/guard.py   # guard: 91 cases, exit 0
python3 -I tests/selftest.py tests/mutant_noop.py      # must exit 1
tests/installer-e2e.sh                                 # sandbox CLAUDE_CONFIG_DIR + throwaway dirs, 70 checks
```
