---
name: decision
description: Record an owner's ruling or an allowed difference in the task's DECISIONS.md, quoted and dated, with a stable id. Use right after the user answers a product or scope question, or approves an expected change.
argument-hint: "<short question or change>"
---

Record a decision for: `$ARGUMENTS`.

1. File: `.claude/devproc/tasks/<CURRENT>/DECISIONS.md`. No CURRENT file: ask the user to run `/devproc:start` first, or to name the task folder.
2. Only the owner's own words count. If the user has not answered yet, ask first: the question, 2-4 options, your recommended option marked. Do not record your own guess as a ruling.
3. Next id: highest existing `D-nn` + 1, two digits.
4. Append under "## Rulings":
   ```
   ### D-nn (YYYY-MM-DD): <question>
   - Options: ...
   - Recommended: ...
   - Answer (quoted): "<owner's exact words>"
   ```
   For an allowed difference, also append one row to the "Expected changes" table with the same date and "approved by".
5. Never edit an older entry. A change of mind is a new dated note under the old entry: `- YYYY-MM-DD amended: "<words>" (see D-mm)`.
6. Read the file back and show the appended lines.
