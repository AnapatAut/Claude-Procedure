---
name: start
description: Start a task under the devproc procedure - pick the profile, create the task folder (TASK.md, DECISIONS.md, run/) and plan the baseline. Use when beginning a bug fix, feature, refactor, migration or release in a devproc project.
argument-hint: "<task-slug> [Light|Standard|Heavy] [ticket]"
disable-model-invocation: true
---

Start a devproc task. Arguments: `$ARGUMENTS` (slug, optional profile, optional ticket id).

1. Project root is the directory Claude Code was started in (the current working directory). If `.claude/devproc/config.json` is missing there, stop: devproc is not installed in this directory; tell the user to run `install.sh --apply <dir>`.
2. Profile: use the argument if given. Otherwise recommend one and ask:
   - Light: bug fix, copy or config change, small UI change.
   - Standard: feature, API change, refactor inside one service.
   - Heavy: backend or DB swap, schema migration, large refactor, multi-week branch, release to prod.
3. Create `.claude/devproc/tasks/<today YYYY-MM-DD>-<slug>/` with:
   - `TASK.md` and `DECISIONS.md` from `${CLAUDE_PLUGIN_ROOT}/templates/`, placeholders `{{TITLE}}`, `{{DATE}}`, `{{PROFILE}}`, `{{TICKET}}` filled in;
   - an empty `run/` directory.
   Never overwrite an existing task folder.
4. Write the folder name to `.claude/devproc/CURRENT` (one line). Other devproc skills read it.
5. Fill "Scope", "Units" and "Baseline plan" in `TASK.md` from what the user said; leave unknowns as questions.
6. Ask which mode applies: step-gated (default) or autonomous. For autonomous, change the `Mode:` line in `TASK.md` to `autonomous` and fill the mandate section from the user's own words before any work starts.
7. Present the task folder path, the profile, the mode, and the baseline you will capture first (`/devproc:baseline`). Then stop.
