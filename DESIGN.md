# devproc: the general procedure as Claude Code config (design, 2026-10-24)

Usage: `README.md`. The procedure it packages: `docs/PROCEDURE.md`.

Design choices:
- **Per directory, never global.** Each project's procedure evolves on its own, so the rules are seeded once per directory and owned by the project, and the plugin is enabled with `--scope local`.
- **Push guard asks, never denies a clean push.** The owner gives the go each time. Only a leak hit is a hard deny.

Research: live docs at code.claude.com/docs/en/ (memory, skills, sub-agents, hooks, hooks-guide, settings, permissions, output-styles, claude-directory, plugins/create, plugins/install, plugins/manifest-reference, plugins/loading, plugins/security), fetched 2026-10-24. Local Claude Code: 2.1.306.

## 1. What the docs say (facts that shape the design)

- CLAUDE.md and rules are **advisory context**, not enforcement. The docs say: to block an action, use a PreToolUse hook.
- `.claude/rules/*.md` exists (recursive). No `paths:` = loads at launch. `paths:` frontmatter = loads only when matching files are touched. User level `~/.claude/rules/` loads in every project without an approval dialog.
- Size: CLAUDE.md under 200 lines; imports do not reduce context cost. SKILL.md under 500 lines; a skill body loads only when invoked.
- **A plugin cannot ship CLAUDE.md or rules** ("isn't loaded as context... put them in a skill"). A plugin's `settings.json` takes only `agent` and `subagentStatusLine`, so no permissions either. A plugin can ship skills, agents, hooks, output styles, MCP, `bin/`.
- Hooks: PreToolUse with matcher `Bash` gets `tool_input.command` on stdin. Exit 2 = block (stderr is the reason). JSON `hookSpecificOutput.permissionDecision` = `allow|deny|ask|defer`. A hook `allow` never bypasses deny or ask rules. `onFailure: "block"` exists since 2.1.295.
- Permission rule `Bash(git push *)` misses `git -C . push`, `/usr/bin/git push`, quoting tricks. The docs: Bash patterns are not a security boundary. A hook is the stronger gate, still best-effort.
- Settings arrays merge across scopes (concatenate). Deny beats allow from any scope. Project `allow`, `env`, `additionalDirectories` wait for workspace trust; `deny` and `ask` apply at once.
- Subagents load CLAUDE.md (unless `omitClaudeMd`) but not the conversation or invoked skills. Explore and Plan skip CLAUDE.md.
- Output styles replace the default coding instructions unless `keep-coding-instructions: true`, do not reach subagents, and are advisory. Not used here.
- Security: a plugin runs arbitrary code as the user. Hooks run outside the sandbox with the user's environment.

## 2. Mapping: procedure part → Claude Code mechanism (as built)

| Part | Mechanism | Why |
|---|---|---|
| P1 precedence, P20 evidence, P9 claims | rule `00-precedence.md` | always-on; a project's own procedure doc wins over this base |
| P13 modes + stop list, P12 short form | rule `10-modes.md` + skill `/devproc:present` (full template) | short form always loaded; template on demand |
| P17 git | rule `20-safety.md` + hook `plugin/scripts/guard.py`: ask on every `git push` spelling | enforcement belongs in a hook |
| P8 leak grep | same hook: before a local push, leak scan of the commits not on the target remote (merge commits included), hit = deny; pushes it cannot pin to a local repo get an ask that says the scan was not run; `--scan-worktree` (uncommitted + untracked) for the verify skill | must happen, so a hook |
| P18 DB safety | rule `20-safety.md` + same hook: ask on DB writes, migration commands, MCP DB tools | best-effort only; real guard = read-only DB role |
| P3 baseline, P2 oracle | skill `/devproc:baseline` (capture before, compare after, into `run/`) | on demand |
| P4, P15 decisions | skill `/devproc:decision` (append to the task's `DECISIONS.md`, quoted, dated, `D-nn`) | on demand |
| P14 mistakes | skill `/devproc:mistake` (ledger row + ladder) + rule `30-work.md` | on demand |
| P11 re-verify | skill `/devproc:verify` (commands from `.claude/devproc/config.json`, raw output to `run/`) | on demand |
| P9 fresh review | agent `devproc-reviewer` (Read, Grep, Glob, Bash; read-only by instruction, Bash could write) | fresh context is the point |
| P9 can-it-fail | `tests/selftest.py` must pass on the guard and fail on `tests/mutant_noop.py`; the installer runs both | — |
| P16 tracker close | skill `/devproc:close` (draft only; posts on the owner's go) | on demand |
| P5, P20 kit | skill `/devproc:start` (profile, mode, task folder `TASK.md` / `DECISIONS.md` / `run/`) | on demand |
| P6, P7, P10, P19 | not packaged; text only in `docs/PROCEDURE.md` | task-specific tooling |

## 3. Packaging (as built: per directory)

Plugins cannot carry rules, so devproc has two parts, both scoped to one target directory:

1. **Plugin `devproc@devproc`** from the local directory marketplace at the repo root (`.claude-plugin/marketplace.json`, plugin in `plugin/`).
   - It is added and installed with `--scope local` from inside the target. That writes `extraKnownMarketplaces` and `enabledPlugins` to `<target>/.claude/settings.local.json`.
   - Claude Code copies the plugin into its cache, `~/.claude/plugins/cache/devproc/devproc/<version>/`. Installed directories run that copy, not the source folder. A source change reaches a directory only when `version` in `plugin/.claude-plugin/plugin.json` is raised and `install.sh --upgrade --apply <dir>` is run (e2e-tested).
   - The marketplace registration in `~/.claude/plugins/known_marketplaces.json` is shared by all directories. `claude plugin marketplace remove` deletes it, so uninstall uses that command only for the last directory; otherwise it removes just this directory's declaration (e2e-tested).
2. **Seeded files**, copied once and owned by the project afterwards:
   - `<target>/.claude/rules/devproc/` (4 rule files, about 40 lines);
   - `<target>/.claude/devproc/config.json` and `MISTAKES.md`.
3. **Manifest** at `<target>/.claude/devproc/manifest.json`. It records:
   - each managed file, with its sha256 and its origin: `seeded` = written by devproc, `adopted` = already identical to the template, so the project's own, never updated or removed;
   - the directories devproc created;
   - the git exclude lines added;
   - whether `settings.local.json` existed before;
   - whether devproc enabled the plugin.
4. **Where it can go:** inside git, the target must be the top level of the main checkout. Claude Code writes local-scope plugin settings there, even when the CLI runs in a subdirectory or a linked worktree (sandbox-tested). The rule files would then land somewhere else, so the installer refuses those targets and names the right one.

Nothing is written to `~/.claude/settings.json` or `~/.claude/rules/`.

## 4. Installer rules (as built: `lib/devproc_install.py`, wrappers `install.sh`, `uninstall.sh`)

- **Dry run by default:** prints every action. `--apply` does it. `--status` never writes.
- **Refuses:**
  - `/`, `$HOME`, the devproc source itself;
  - a missing or unwritable target;
  - on install: a repo subdirectory or a linked worktree (the message names the main checkout top).
- **Preflight, before any write:**
  - python3 >= 3.8, plus `git` and `claude` on PATH;
  - `tests/selftest.py` passes on the guard and fails on the mutant;
  - `claude plugin validate` passes for the plugin and the marketplace;
  - an existing `settings.local.json` is valid JSON.
- **Never replaces a file it did not write:**
  - a file that exists and differs from the template is left alone and reported;
  - a file identical to the template is recorded as `adopted`;
  - in `settings.local.json` and the exclude file, devproc only adds and removes its own entries.
- **Git:** in a git work tree, devproc paths not already ignored are added to the local exclude file, each under a marker line. The exclude file is never committed.
- **Idempotent:** a second install sees the manifest and changes nothing.
- **Install and upgrade roll back** on a failed step, undoing completed steps in reverse:
  - files are restored to their previous bytes (removed if new);
  - created directories are removed if empty;
  - plugin and marketplace entries are undone (the shared registry entry is kept when other directories use it);
  - a `settings.local.json` the CLI created is removed if empty;
  - the manifest is restored.

  Not undone: `claude plugin update` in an upgrade (rerun the upgrade). A failed undo is reported as `rollback INCOMPLETE`.
- **Post-check:**
  - the plugin at the expected version is enabled for this directory in `claude plugin list --json`;
  - the self-test passes on the installed copy of the guard.
- **Upgrade:**
  - re-adds the marketplace (idempotent; it repairs a registry entry);
  - updates seeded files that are still as seeded;
  - keeps evolved and adopted files, and prints the `diff -u` command for evolved ones;
  - seeds new templates;
  - updates the marketplace and the plugin.
- **Uninstall** has no rollback: it stops at a failed step, says so, and a rerun finishes it. It:
  - uninstalls the plugin;
  - removes the marketplace (for the last directory) or only this directory's declaration;
  - removes seeded files unchanged since install;
  - removes exclude lines only for paths where no file stays behind, so kept task data and evidence stay ignored;
  - removes `settings.local.json` only if devproc created it and it is now empty;
  - removes the manifest, and the directories devproc created, if empty;
  - lists every kept file.
- **Hook script:** stdlib Python, no network, writes nothing, never answers "allow". Git calls time out at 20 s; the hook timeout is 150 s.
- **Per user:** every user runs it as themselves; installs are per user and per directory.

## 5. Fit with an existing setup

- Your own `ask` or `deny` rules on `git push` in `settings.json` stay. A hook `allow` never overrides them, and devproc never answers `allow`. The hook adds the spellings that permission patterns miss.
- devproc uses only the PreToolUse event, so it does not clash with plugins that hook other events.
- Every permission mode, including `auto`, still prompts on a hook "ask". In headless `claude -p`, an "ask" becomes a refusal.
- A project that already has its own procedure keeps it: rule `00-precedence.md` says the project's own procedure wins over this base.
- Inside git, only the top level of the main checkout can be a target; a linked worktree points to its main checkout.

## 6. Test evidence (2026-10-24, version 0.2.0)

- **`tests/selftest.py`:**
  - 91/91 cases pass on `plugin/scripts/guard.py` 0.2.0;
  - it fails on `tests/mutant_noop.py` (exit 1, as required);
  - on the first, unpublished guard 0.1.0, 22 of the 84 cases of the time failed, each reproducing a gap from the fresh-context review.
- **`tests/installer-e2e.sh`:** 70/70 checks pass, with a sandbox `CLAUDE_CONFIG_DIR` and throwaway targets. On a copy with `remove_exclude` disabled, 2 checks go red. The newer checks caught real bugs before the fixes:
  - uninstalling one directory broke another;
  - a worktree install wrote its settings into the main checkout.
- **Live interactive session** (`claude --plugin-dir plugin`, auto mode, guard 0.1.0):
  - `/usr/bin/git push` stopped with "Hook PreToolUse:Bash requires confirmation … devproc (P17) … leak scan clean (1 commit(s) scanned)";
  - the push was declined, and the bare remote is unchanged.
- **Live `claude -p --plugin-dir plugin`:**
  - guard 0.1.0: a push with a planted AWS-key pattern was denied, naming the file, line and pattern, without the value; a clean push was not run without approval;
  - guard 0.2.0: `bash -c 'git push origin HEAD:main'` with a planted AWS-key pattern was denied (0.1.0 only asked); nothing pushed.
- **Skill substitution:** a live session loading `/devproc:verify` received `${CLAUDE_PLUGIN_ROOT}` expanded to the plugin path.
- **Real config:** `~/.claude/settings.json`, `installed_plugins.json` and `known_marketplaces.json` have unchanged hashes across all tests.
- **Fresh-context reviews:** a first review of all docs against the code found 25 issues; 0.2.0 fixes the code issues, and the docs state the rest as limits. A second review before publishing found no leaks and about 20 doc and adoption gaps, all fixed.
