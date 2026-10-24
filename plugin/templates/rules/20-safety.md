<!-- Seeded by devproc. This project owns this file: edit it as the project's procedure evolves. -->
# Procedure: git, DB and guards

- Push and deploy only on an explicit go, each time. The devproc hook asks on every `git push` spelling it recognises. For a local push it scans the commits not yet on the target remote and blocks the push on a leak pattern hit (`.claude/devproc/config.json` adds project patterns). For pushes it cannot pin to a local repo (ssh, `GIT_DIR`, `--git-dir`) it asks and says the scan was not run: then check by hand.
- Ticket id in every commit subject when the project uses tickets. Stage explicit paths; read the staged column before committing. Never `git add -A` on a dirty tree.
- Doc sweep before each commit: any doc that says "calls / owns / at file:line" about code you moved is checked.
- Shared databases are read-only by default: `SELECT` only. A write, migration or data load needs a written, scoped approval for that step, a pre-image, and a verify step. A change that seems to need a schema change stops and becomes its own decision. The hook is a backstop, not the guarantee: use a read-only DB role.
- Protection only grows: never delete or gut a test to go green; a removal needs a written reason in the same commit. Temporary code carries a `TEMP-REMOVE` marker; when you add one, add a line under "Temporary code" in the task's `TASK.md`. The task closes only when that list is empty and `git grep -n TEMP-REMOVE` finds nothing. Account for every deleted symbol before committing.
- Leak scan the changed files before every presentation; `/devproc:verify` runs it.
