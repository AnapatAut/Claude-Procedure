---
name: present
description: Present a finished devproc step in the fixed format (diff, checks, mistakes, SAFE/CORRECT/SECURE, test steps, out-of-scope) and stop. Use at the end of every step, before asking for approval or a commit.
---

Present the step in exactly these sections, then end the turn.

1. **Changes**: the diff hunks (changed lines plus 1-2 lines of context per hunk), file by file.
2. **Checks**: the table from the last `/devproc:verify` run - check, exit code, summary, log path. Rerun it if code changed since.
3. **Mistakes this step**: ledger rows added, with status labels, or "none".
4. **SAFE / CORRECT / SECURE**, one line each:
   - SAFE: what could break for users or data, and why it does not.
   - CORRECT: which check or baseline compare proves the behaviour.
   - SECURE: auth, roles, tenant isolation, secrets, input handling touched or not.
5. **Test it yourself**: numbered manual steps with full URLs or exact commands.
6. **Decisions needed**: questions for the owner, each with a recommended option, or "none".
7. **Out of scope**: things noticed but not done, each with a proposed owner or ticket, or "none".

Every number, SHA and `file:line` must come from a command run in this step. Do not commit, push or set anything to Done. Wait for the user.
