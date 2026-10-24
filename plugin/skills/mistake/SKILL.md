---
name: mistake
description: Log a mistake in .claude/devproc/MISTAKES.md and apply the recurrence ladder. Use as soon as you notice you made a mistake (wrong claim, skipped check, wrong file, broken rule), or the user points one out.
argument-hint: "<what happened>"
---

Log the mistake: `$ARGUMENTS`.

1. File: `.claude/devproc/MISTAKES.md`.
2. Find the rule it breaks in "## Rules". None fits: add a new rule line `M-nn: <rule> - bites when <situation>`.
3. Append one ledger row: next `A-nnn` id, date, task folder name from `.claude/devproc/CURRENT`, rule id, what happened in one sentence, status (`[OPEN]` until fixed; `[FIXED — verified]` only with the command that proves it).
4. Count earlier ledger rows with the same rule id:
   - 0: done.
   - 1 (first repeat): in this same step, automate it (script, test, check) or add a named line to `.claude/rules/devproc/30-work.md`. Say which.
   - 2 or more: the step cannot close until the owner acknowledges this row in writing. Say so in the presentation and stop.
5. Never edit or delete an earlier row; corrections are new rows.
