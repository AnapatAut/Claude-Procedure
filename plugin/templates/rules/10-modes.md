<!-- Seeded by devproc. This project owns this file: edit it as the project's procedure evolves. -->
# Procedure: control modes and stop list

- Default mode is step-gated: explain the step, get approval, apply, verify, present, STOP. "Proceed" means apply, verify and present. It never means commit.
- Autonomous runs happen only under a written mandate in `TASK.md` naming: the range, the stop list, the retry budget (default 2 fix attempts per red check), whether commits need a word. Push never happens without a go.
- Stop at once, in both modes, on:
  1. a security finding (record it first; continue only if the mandate says so and it does not touch the code being changed);
  2. any write to a shared or production database;
  3. push, deploy, or setting a tracker item to Done;
  4. new behaviour a user would see that is not on the allowed-differences list;
  5. a check still red after the retry budget;
  6. a product question nobody has ruled on;
  7. an environment failure (out of memory or disk, service down).
- Non-critical choices: take the recommended option and record it in the close.
- Every step ends with the presentation (skill `/devproc:present`): diff hunks, check results with exit codes, mistakes made, SAFE / CORRECT / SECURE one line each, manual test steps with full URLs, decisions needed (each with a recommended option), out-of-scope finds with an owner or "none". Then the turn ends.
