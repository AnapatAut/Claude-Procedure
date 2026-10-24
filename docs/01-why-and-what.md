# 1. Why and what

## The problem

When Claude Code works on a task, the same failures keep coming back:

- **Claims from memory.** A number, SHA or `file:line` in a status post that no command produced.
- **Checks that cannot fail.** A new test that passes whether or not the code is right.
- **Baselines taken after the edit.** The "expected" output already contains the bug.
- **Silent behaviour changes.** A difference nobody approved, because nobody wrote down which differences were allowed.
- **Outward actions too early.** A commit before review, a push without a go, a write to a shared database.
- **Lessons that do not stick.** The same mistake in the next task, because it lived only in chat.

A heavy procedure stopped these failures on a large backend migration. `PROCEDURE.md` is its general version for everyday work: bug fixes, features, refactors, migrations and releases. devproc turns the core of it into Claude Code config, so a project gets it by running one command.

## What a project gets

| Piece | Kind | Effect |
|---|---|---|
| 4 rule files, about 40 lines | always-on instructions | precedence, step-gated mode and stop list, git and DB safety, baseline / verify / mistakes / decisions |
| Guard hook | enforcement | asks before any `git push`; blocks a local push whose new commits carry a secret; asks before DB writes and migrations |
| 7 skills | slash commands | `/devproc:start`, `baseline`, `verify`, `decision`, `mistake`, `present`, `close` |
| Reviewer agent | subagent | a fresh-context check of a step against the rules and the evidence, read-only by instruction |
| `MISTAKES.md`, task folders | files | the mistake ledger, plus per-task `TASK.md`, `DECISIONS.md` and `run/` evidence |

All of it is per directory. A project without devproc is not affected.

## What it does not do

- **It is not a security boundary.** The hook catches the common spellings of `git push` and of DB writes. It cannot see a push hidden inside a script file, a `gh` command, or a shell alias. It cannot scan a push it cannot pin to a local repo (ssh, `GIT_DIR`): then it asks and says so. Only a read-only database role truly protects a database.
- **It does not replace CI.** `/devproc:verify` runs your project's own commands; it has no test runner of its own.
- **It does not ship heavyweight tooling** such as checker suites or close scripts. The procedure says to build tooling like that only when a mistake recurs.
- **Outside the project it only registers the plugin** with Claude Code (its plugin registry and cache under `~/.claude/plugins/`). It never edits your Claude settings file.
