<!-- Seeded by devproc. This project owns this file: edit it as the project's procedure evolves. -->
# Procedure: baseline, differences, verify, mistakes, decisions

- Baseline before the first edit (skill `/devproc:baseline`): save the output of what the change touches. Never regenerate a baseline or golden file after the edit to make it pass.
- Any difference from the baseline is a regression unless it is listed under "Expected changes" in `DECISIONS.md`, approved by the owner. Structure changes and behaviour changes go in separate commits.
- Before every presentation run `/devproc:verify`: typecheck, tests (the runner CI uses), lint the touched files, build if the app changed, `git status` shows only intended files, leak scan, and a backend error surfaces as an error, never as an empty success. Scripts: `set -euo pipefail`; never read an exit code after `| tail`.
- Mistakes go to `.claude/devproc/MISTAKES.md` (skill `/devproc:mistake`). First repeat of a rule: automate it or add a checklist line here. Second repeat: the step cannot close until the owner acknowledges it in writing.
- Decisions (skill `/devproc:decision`): asked with a recommended option, recorded with the owner's words quoted, the date and an id. Old decisions are never rewritten; changes are appended as dated notes.
