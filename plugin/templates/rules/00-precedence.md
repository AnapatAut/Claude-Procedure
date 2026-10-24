<!-- Seeded by devproc. This project owns this file: edit it as the project's procedure evolves. -->
# Procedure: precedence and evidence

- Order when documents disagree: the owner's rulings in `.claude/devproc/tasks/<task>/DECISIONS.md` > task plan (`TASK.md`) > this project's own procedure docs (if any) > these rules > repo `CLAUDE.md` > memory. Memory holds pointers to rules, not rules.
- Re-read the rules for the current step from the files, never from memory.
- Every number, SHA, date and `file:line` you post comes from a command run just before posting. Count by parsing, never by `grep -c` over free text.
- Raw output of every check goes to the task's `run/` directory. Chat and tracker posts carry the command, exit code, one summary line and the path.
- A check that cannot fail proves nothing: before trusting a new test or script, break what it guards, see it go red, restore it.
- Evidence order: end-to-end run > a test proven by a planted defect > suite green.
