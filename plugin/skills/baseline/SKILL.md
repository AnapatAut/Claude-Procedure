---
name: baseline
description: Capture a devproc baseline before an edit, or compare against it after the edit. Use before the first edit of a task and again after the change, for any command, endpoint or page output the change can affect.
argument-hint: "capture <label> -- <command> | compare <label>"
---

Baseline step. Arguments: `$ARGUMENTS`.

Task folder: `.claude/devproc/tasks/<name in .claude/devproc/CURRENT>/`; evidence goes in its `run/`. No CURRENT file: ask the user to run `/devproc:start` first.

**capture** (before any edit of the code it covers):
1. Refuse if `run/baseline-<label>.out` already exists. A baseline is never replaced; use a new label.
2. Run the command once with `set -euo pipefail`. Save stdout+stderr to `run/baseline-<label>.out`, and a header file `run/baseline-<label>.meta` with: date-time with offset, `git rev-parse HEAD`, `git status --porcelain | wc -l`, the exact command, the exit code.
3. If the output has volatile parts (timestamps, ids, ordering of unordered sets), say so and propose a mask. To measure noise, run the command once more into a scratch file and diff it against the baseline.

**compare** (after the edit):
1. Run the same command (read it from the .meta file, not from memory). Save to `run/after-<label>-<HHMMSS>.out`.
2. `diff -u` the baseline and the new output, with only the masks agreed during capture (capture step 3), into `run/diff-<label>-<HHMMSS>.txt`.
3. Every difference must match a row under "Expected changes" in the task's `DECISIONS.md`. Any other difference is a regression: fix the code (never the baseline) or ask the owner for a ruling and record it with `/devproc:decision`.

Report: command, exit codes, number of differing lines, how many are explained by which DECISIONS row, path of the diff.
