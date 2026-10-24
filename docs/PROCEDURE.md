# devproc procedure: 20 parts for everyday dev work

This is the procedure devproc packages. It is the general form of a heavier method first used on a large backend migration. That project was the heaviest case. Most tasks need less: take the idea of each part, and scale it to the task.

It applies to a bug fix, a feature, a refactor, a migration, an infra change and a release.

## How to read this

Each part gets a verdict:
- **CORE**: use on every task, in its light form.
- **SCALED**: use when the task's risk calls for it (see the profiles below).
- **SPECIFIC**: built for the original heavy project; take only the general idea.

Then four headings:
- **Rule:** the part as a general rule.
- **Light form:** the cheapest version that still works.
- **Scale up when:** signs that the full form is needed.
- **Leave out:** detail of the original heavy project that does not carry over.

## Three profiles

| Profile | Typical task | Parts |
|---|---|---|
| Light | bug fix, copy change, small UI change, config tweak | P1, P3, P8, P11, P12, P13, P14, P17, P20, plus the "can it fail" test from P9 |
| Standard | feature, API change, refactor inside one service, data read change | Light + P2, P4, P5, P6 (API level, and UI level when the UI can differ), P9 (claim check, fresh review), P15, P16, P18 |
| Heavy | backend or DB swap, schema migration, large refactor, multi-week branch, release to prod | all 20 |

Pick the profile at the start and write it in the task file. Move up a profile as soon as one "scale up when" line is true. Never move down in the middle of a task.

---

## P1. Rule sources and precedence — CORE

**Rule:** one written rules file decides HOW; a separate list decides WHAT may change. State the order in which the documents win. Re-read the rules for the current step from the file, never from memory.

**Light form:** a short "Rules and precedence" block at the top of the task file or the repo `CLAUDE.md`. One line of order, for example: "DECISIONS.md > task plan > PROCEDURE.md > domain rules > repo CLAUDE.md > memory." The document names change per task; the roles and their order do not. Memory holds pointers to rules, not the rules.

**Scale up when:** the task spans weeks, several agents or sessions, or more than one document gives rules.

**Leave out:** a separate document per rule type, unless the task is large. Keep the size in check: put the HOW body under about 300 lines and move rulings into their own log, or the file outgrows re-reading (the original project's procedure file grew far too large to re-read).

## P2. Oracle and candidate — SCALED

**Rule:** compare the change against a reference you do not touch. The oracle is the current behaviour: the deployed environment, or a build of the main branch. The candidate is your branch. Both sides get the same inputs. No candidate flow may touch the oracle's services or state.

**Light form (bug fix):** run the repro on main (oracle) and on the branch (candidate). Save both outputs.

**Standard form:** a local candidate stack next to a reference environment; pin the oracle's SHA at the start and end of a run; act only as test accounts.

**Scale up when:** the change can alter data, auth or money; a hidden dependency could reach the oracle (shared DB, queue, cache, third-party account).

**Leave out:** machine-specific service units and tunnels.

## P3. Baseline before the edit — CORE

**Rule:** capture the expected output before you edit, from the pre-edit state. Never edit it afterwards. After the change, the output must equal the baseline value for value, except for the differences you listed (P4).

**Light form:** before the first edit, save the output of the affected command, endpoint or page: `curl … > before.json`, a test run log, a screenshot. Golden files, snapshot tests and approval tests are the same idea.

**Scale up when:** the change touches many routes or pages; writes are involved (then make the baseline writes state-preserving, or test writes only on throwaway rows); shared state can move under you (take the reference after the last write).

**Leave out:** baseline manifests and hashes for small tasks. Replaying recorded writes needs care only for write captures on a shared DB: re-snapshot first.

**Never:** regenerate a golden file after the edit to make it pass. That absorbs the bug.

## P4. List of allowed differences — CORE for refactors, SCALED for features

**Rule:** any difference from the baseline is a regression unless it is on a written list, approved by the person who owns the product decision.

**Light form:** a section in the PR or task file: "Expected changes: … Everything else must match." For a feature, the expected changes are the spec.

**Standard form:** a machine file the diff tool reads (route or page, field, reason, approver, date). "Not listed = red."

**Scale up when:** the diff is large or noisy; several people approve changes; timestamps, counters or test data differ by design (then add narrow row types such as "timestamp may differ" or "counter may grow", each with exact conditions).

**Leave out:** elaborate row-type numbering and a separate tracker register for small tasks.

**Keep:** the two-phase rule for risky moves: first change the structure with the same behaviour, then change behaviour in a separate step.

## P5. Work structure — SCALED

**Rule:** cut the work into units small enough to prove one at a time. Each unit runs inventory → change → verify → close, and each step has an exit.

**Light form:** one unit with two steps: inventory (what it touches) and change + verify.

**Standard form:** units by component or table group, with a dependency order.

**Heavy form:** seam first. Put the old code behind an interface with byte-identical behaviour, prove it, then add the new implementation behind the same interface (branch by abstraction, strangler fig).

**Scale up when:** more than one table, service or team is touched; the branch will live longer than a few days.

**Leave out:** stage and slice numbering for small tasks. Keep "separate modes in separate files" only where your product has modes that must not cross.

## P6. Cross-check layers — SCALED

**Rule:** compare oracle and candidate at every layer the user sees, with tolerances you measured, and resolve every red in a fixed order.

**Light form:** API level only: same requests to both sides, byte-diff of status and body; parse only the fields you name.

**Standard form:** add the UI layer when the frontend can differ: headless browser, same pages and accounts, compare requests, console errors, error boundaries, text and screenshots. Measure the noise first by comparing the oracle with itself; set tolerances from that measurement.

**Heavy form:** add a risk checklist filled on every gate, one row per drift class, "n/a + reason" allowed but never blank. General rows:
1. Response shape, value, order, headers.
2. Error contract and validation per verb and role.
3. Auth and role per verb.
4. Tenant isolation.
5. Ordering.
6. Caching and cache busts.
7. Concurrency and idempotency.
8. Atomicity on failure (no partial state).
9. DB side effects (triggers, constraints).
10. Time and day boundaries.
11. Env and boot config.
12. Docs and instruction files.
13. Latency.
14. A planted defect turns the gate red.

**Resolution order:** (1) bug in your change → fix and rerun; (2) bug in data or tooling → fix there and rerun; (3) on the allowed list → cite it; (4) unknown → stop and ask. Never adapt the frontend to hide a backend difference. Never add a list row inside a gate to make it pass.

## P7. Refactor-equivalence proofs — SCALED (refactors only)

**Rule:** a pure refactor is proven equal by a script, not by reading. A behaviour fix never rides a refactor commit.

**Light form:** keep refactor and fix in separate commits. Run the same baseline on both commits.

**Standard form:** compare a comment-free build or transpile output of the touched files before and after, or an AST-level comparison of the moved code. Plant one defect to prove the comparison sees it.

**Scale up when:** error paths the tests cannot reach are being moved.

**Leave out:** tools that compare query chains; they are specific to one query builder.

## P8. Guards and ratchets — CORE

**Rule:** protection may only grow.
- Never delete or gut a test to go green. Track test files and their size in CI; a removal needs a written reason in the same commit.
- Known-fail ratchet: when a suite has old failures, CI is red only on a NEW failing test.
- Temporary code carries a marker and a registry row in the same commit; the task ends when the registry is empty.
- Leak grep before every push: local hostnames, ports, keys, personal paths.
- Account for every deleted symbol before the commit.

**Light form:** the leak grep, "no test deleted", and the deletion check, done by hand with commands.

**Scale up when:** the suite has known failures, or the branch carries temporary code: add the ratchet and the marker registry.

## P9. Checks on your own work — CORE idea, SCALED tools

**Rule (CORE):** a check that cannot fail proves nothing. Before you trust any new test, gate or script, break the code it guards and see it go red; then restore and record the file hash to prove the restore.

**Light form:**
- Every number, SHA, date and `file:line` in a PR description or status post comes from a command run just before posting, not from memory. Counts are taken by parsing, never by `grep -c` over free text.
- Read the ticket's own claims against the code before starting; write corrections down.
- Every TODO or "later" names a ticket.

**Standard form:** a fresh reviewer agent with no session context reads the rules file, the diff and the evidence, and lists gaps; every finding gets an answer (fixed, answered, out of scope with an owner, or conflict).

**Scale up when:** the task posts many facts to a shared tracker, or several sessions work on it.

**Leave out:** a large suite of checkers built up front (the original project grew to dozens of tool files). Build a checker only when the same mistake recurs (P14 ladder).

## P10. Step checklists and close tooling — SPECIFIC

**Rule:** for long autonomous runs only: a checklist file per step with evidence paths, tool-written timing lines, and report numbers filled from a facts script.

**Light form:** a checklist in the task file; tick each line with the path of its evidence.

**Scale up when:** an agent runs many steps without a human between them.

**Leave out:** dedicated close tooling (close runners, report renderers, timing linters). It paid off only on a project with many units and steps.

## P11. Re-verify checklist — CORE

**Rule:** run a fixed list before every presentation.
- Typecheck; test suite with the runner CI uses; lint the touched files; build if the app changed.
- `git status` shows only the intended files; read the staged column.
- Leak grep on the changed files.
- Failure-mode check: a backend error surfaces as an error, never as an empty success.
- Shell hygiene: `set -euo pipefail`; never read an exit code after `| tail`; quoted heredocs.
- Raw output to a file; chat carries command, exit code, summary, path.

**Light form:** the list as is; it costs minutes.

## P12. Presentation contract — CORE

**Rule:** every step ends with the same report, then a stop.
- Diff hunks.
- Check results with exit codes.
- Mistakes made in this step, with status labels.
- SAFE / CORRECT / SECURE, one line each.
- Manual test steps with full URLs.
- Decisions needed from the owner, or "none".
- Out-of-scope finds at the end, each with a proposed owner, or "none".

## P13. Control modes — CORE

**Rule:** decide the mode before the work starts and write it down.
- **Step-gated (default):** explain, get approval for this step, apply, verify, present, stop. "Proceed" means apply, verify and present; it never means commit.
- **Autonomous run:** only with a written mandate that names the range, the stop list, the retry budget, whether commits need a word, and that push never happens without a go.

**General stop list:**
1. Security finding (record it at once; continue only if the mandate says so and the finding does not touch the code being changed).
2. Any write to a shared or production DB.
3. Push, deploy, or closing an item as Done.
4. New behaviour a user would see that is not on the allowed list.
5. A check still red after two fix attempts.
6. A product question nobody ruled on.
7. Environment failure (out of memory or disk, service down).

**Both modes:** non-critical choices take the recommended option and are recorded in the close.

## P14. Mistake system — CORE

**Rule:** every mistake is written down, and a mistake that repeats becomes part of the procedure.
- Rules file: one line per lesson, with where it bites.
- Ledger: one row per mistake, append-only, with a status label (`[FIXED — verified]`, `[FIXED — unverified]`, `[OPEN]`, `[NO-IMPACT / NOTED]`).
- Ladder: first repeat → automate it (script, test, template) or add a checklist line; second repeat → the step cannot close until the owner acknowledges it in writing.

**Light form:** one `MISTAKES.md` per repo or per long task. The ladder still applies.

## P15. Rulings capture — CORE

**Rule:** decisions are asked with a recommended option, answered by the owner, and recorded with their words and the date under a stable id. Old rulings are never rewritten; changes are appended as dated notes.

**Light form:** a "Decisions" section in the task file: date, question, answer (quoted), id.

**Scale up when:** a run will be autonomous: rule the predictable questions before it starts, and batch the rest per unit.

## P16. Tracker logging — CORE

**Rule:** the tracker item is the changelog.
- One item per unit; one place for status.
- A close comment with fixed sections: header (SHA, branch, time with offset), what shipped, evidence, corrections to the plan, still open with owners.
- Numbers checked before posting (P9). No local paths or hostnames in shared text.
- State rules: Ready for Review only with the evidence posted; Done is set by a human.
- Write, then read back.

**Light form:** the close comment sections and the read-back.

**Leave out:** tracker API details and team-specific posting rules.

## P17. Git and branch — CORE

**Rule:**
- Ticket id in every commit subject; stage explicit paths; read the staged column.
- Doc sweep before every commit: any doc that says "still calls / owns / at file:line" about what you moved is checked.
- Push and deploy only on an explicit go, each time.
- Catch-up rule: merge the main branch in when commit hashes are cited in proofs or posts; rebase only when nothing cites them.
- Long-lived branch: catch up daily and before every gate; port every main-branch change that touches your area into your branch.

**Light form:** the first three bullets.

## P18. DB safety — CORE

**Rule:** shared databases are read-only by default.
- `SELECT` only, through a read-only path.
- A write, migration or ETL run happens only under a written, scoped approval for that step, with a pre-image and a verify step.
- Staging and prod: four-eyes.
- A change that seems to need a schema change stops and becomes its own decision.

**Light form:** read-only by default; ask before any write.

**Scale up when:** migrations are involved: add a runbook with preflight, apply (`ON_ERROR_STOP`), verify and rollback.

## P19. Promotion and release — SCALED

**Rule:**
- Know what each environment runs by measuring it (schema fingerprint, image or build SHA), not by `git diff`.
- Order: migrations before code; backend before frontend.
- Read-only pre-check and post-check on every box: fingerprint, containers, error lines, probes, smoke.
- Rehearse and time the rollback before the first real flip; soak after it.

**Light form:** the pre-check and post-check on the target box.

**Scale up when:** a migration rides the release, or more than one service changes together.

## P20. Evidence store — CORE

**Rule:** one directory per task and step holds every output a claim rests on. Chat and tracker posts point to it; they do not replace it.

**Light form:** `run/<task>/<date>/` with the raw output of each check, named by command.

**Keep out of the repo and the tracker** any evidence that holds real users' data; restrict its permissions.

---

## Starter kit for a new task

1. Pick the profile (Light, Standard, Heavy) and write it at the top of the task file.
2. Create, next to the work (never committed if it holds local paths or real data):
   - `PROCEDURE.md`: the HOW, with its precedence line, under about 300 lines. Copy the shared base (P8, P11–P14, P17, P18) from the last task; write only the task part new.
   - Task plan: stages and units (P5). Domain rules file only if the task has a domain that needs one.
   - `DECISIONS.md`: the rulings log (P15) and the allowed-differences list (P4).
   - `MISTAKES.md`: rules, ledger, ladder (P14).
   - `run/`: the evidence store (P20).
3. Capture the baseline (P3) before the first edit.
4. Run the loop per step: inventory, change, re-verify (P11), present (P12), stop or continue by mode (P13).
5. Close on the tracker (P16); push and deploy only on a go (P17).

Under devproc these files have fixed places: rules in `.claude/rules/devproc/`, `MISTAKES.md` and `config.json` in `.claude/devproc/`, and per task `.claude/devproc/tasks/<date>-<slug>/` with `TASK.md`, `DECISIONS.md` and `run/`.

## What not to copy

- A procedure file that grows without limit (the original grew far too large to re-read). Keep the HOW short; rulings go in their own log and are pruned when superseded.
- Tooling built ahead of need. Build a checker only when the ladder (P14) says a mistake recurred.
- Project-specific numbering schemes. Use plain names for a new task.
