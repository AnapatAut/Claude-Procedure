---
name: close
description: Draft the tracker close comment for a devproc unit or task (header, what shipped, evidence, corrections, still open) with every number taken from commands. Use when a unit is verified and the user wants to close or report it.
argument-hint: "[ticket id]"
disable-model-invocation: true
---

Draft a close comment for `$ARGUMENTS` (default: ticket in the task's `TASK.md`).

1. Run, and keep the outputs in `run/close-<YYYYMMDD-HHMMSS>/`: `git rev-parse HEAD`, `git branch --show-current`, `date -Iseconds`, `git grep -n TEMP-REMOVE`, the latest `/devproc:verify` results, the baseline diff summary. If `git grep` finds a marker, or the "Temporary code" list in `TASK.md` is not empty, stop: the task cannot close yet.
2. Draft with these sections:
   - Header: SHA, branch, date-time with offset.
   - What shipped: one line per change.
   - Evidence: each check with its result (no local paths, hostnames or user data in tracker text; name the check, not the folder).
   - Corrections to the plan: plan statements that turned out wrong, or "none".
   - Still open: each item with an owner or ticket, or "none".
3. Show the draft to the user. Post it only on their go, and only with a tool they have set up for the tracker. After posting, read the item back and confirm the text landed.
4. Never set an item to Done: that is the owner's call. "Ready for review" only with the evidence posted.
