# devproc: the general procedure as Claude Code config (design, 2026-10-27, version 0.2.3)

Usage: `README.md`. The procedure it packages: `docs/PROCEDURE.md`.

Design choices:
- **Per directory, never global.** Each project's procedure evolves on its own, so the rules are seeded once per directory and owned by the project, and the plugin is enabled with `--scope local`.
- **Push guard asks, never denies a clean push.** The owner gives the go each time. Only a leak hit is a hard deny.

Research: live docs at code.claude.com/docs/en/ (memory, skills, sub-agents, hooks, hooks-guide, settings, permissions, output-styles, claude-directory, plugins/create, plugins/install, plugins/manifest-reference, plugins/loading, plugins/security), fetched 2026-10-24; plugins/loading and plugins/marketplace-reference again 2026-10-25. Local Claude Code: 2.1.306, then 2.1.310.

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
- **Where an installed plugin's code comes from** (docs, plugins/loading, "In-place and copied plugins"): a plugin with a relative-path source in a marketplace added from a local path "loads in place from its path inside the marketplace folder. Your edits to the source directory take effect at the next session start or `/reload-plugins`, and you don't need to increase the version." Claude Code still writes a copy to `~/.claude/plugins/cache/`, but sessions do not load it. Confirmed in a sandbox: `claude plugin list --json` reports `readFromFolder` = the marketplace folder, and a session's debug log shows `Read hooks.json … <marketplace folder>/plugin/hooks/hooks.json`.
- One marketplace per name per user (`~/.claude/plugins/known_marketplaces.json`). `claude plugin marketplace add <path> --scope local` with a name already registered repoints it ("Plugins already installed from it now update from the new source"). `claude plugin marketplace remove <name>` also removes every install record of its plugins. With `--scope local` it is refused unless the name is declared in this directory's `settings.local.json` ("Omit --scope to remove it from all scopes"); without `--scope` it works from any directory.
- A registered marketplace folder that no longer exists: the plugin fails to load (`Marketplace <name> failed to load: cache-miss`) and sessions run without it.

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

Plugins cannot carry rules, so devproc has two parts, both scoped to one target directory.

1. **The plugin, as a read-only snapshot inside the target.**
   - `install` copies `plugin/` to `<target>/.claude/devproc/dist/<version>/plugin/`, writes `dist/<version>/.claude-plugin/marketplace.json` with a name of its own, `devproc-<first 8 hex of sha256(target path)>`, validates it, and makes it read-only.
   - It registers that folder as a marketplace and installs `devproc@devproc-<id>`, both with `--scope local` from inside the target. That writes `extraKnownMarketplaces` and `enabledPlugins` to `<target>/.claude/settings.local.json`.
   - Sessions in the target load the snapshot in place (section 1). So an edit to the devproc source never reaches an installed directory, and each directory has its own version: no registry entry or plugin code is shared between directories.
   - The manifest records the snapshot's tree hash (`pluginHash`). A version's code never changes: an upgrade whose source differs from what the directory has, under the same version, is refused.
   - Why not the alternatives: one shared marketplace (0.2.0) runs the source clone live in every directory, and installing from a second clone repoints all of them. One marketplace per version still shares a version across directories. A plugin folder in `<target>/.claude/skills/` does not load in sessions started in a subdirectory and needs the trust dialog.
2. **Seeded files**, copied once and owned by the project afterwards:
   - `<target>/.claude/rules/devproc/` (4 rule files, about 40 lines);
   - `<target>/.claude/devproc/config.json` and `MISTAKES.md`.
3. **Manifest** at `<target>/.claude/devproc/manifest.json`. It records:
   - `layout` 2, `version`, `pluginHash`, the `marketplace.json` written into the snapshot, the marketplace name and plugin id, the snapshot path, and the target path at install (to detect a moved or copied project);
   - each managed file, with its sha256 and its origin: `seeded` = written by devproc, `adopted` = already identical to the template, so the project's own, never updated or removed;
   - the directories devproc created;
   - the git exclude lines added;
   - whether `settings.local.json` existed before.
4. **Where it can go:** inside git, the target must be the top level of the main checkout. Claude Code writes local-scope plugin settings there, even when the CLI runs in a subdirectory or a linked worktree (sandbox-tested). The rule files would then land somewhere else, so the installer refuses those targets and names the right one. A session started in a subdirectory of the target loads the plugin (sandbox-tested).

Nothing is written to `~/.claude/settings.json` or `~/.claude/rules/`. Outside the target, only Claude Code's own registry and cache change, through the `claude plugin` CLI.

**Correction to 0.2.0.** The 0.2.0 docs said installed directories run a cached copy and a source change reaches them only through a version bump and `--upgrade`. That was wrong: 0.2.0 registered the source clone itself, so every installed directory ran the clone live. The 0.2.0 test checked the cache copy, which sessions never load. 0.2.1 changes the layout; `--upgrade` moves a 0.2.0 directory to it.

## 4. Installer rules (as built: `lib/devproc_install.py`, wrappers `install.sh`, `uninstall.sh`)

- **Dry run by default:** prints every action. `--apply` does it. `--status` never writes.
- **Refuses:**
  - `/`, `$HOME`, the devproc source itself;
  - a missing or unwritable target;
  - on install: a repo subdirectory or a linked worktree (the message names the main checkout top);
  - on install: a target whose `settings.local.json` already enables a `devproc@…` plugin without a manifest, or whose marketplace name is already registered.
- **Preflight, before any write:**
  - python3 >= 3.8, plus `git` and `claude` on PATH; the version string can name a directory;
  - `tests/selftest.py` passes on the guard and fails on the mutant;
  - `claude plugin validate` passes for the plugin and the marketplace;
  - an existing `settings.local.json` is a JSON object, with object-valued `enabledPlugins` and `extraKnownMarketplaces` where present, and is not tracked by git (devproc writes absolute local paths into it).
- **Never replaces a file it did not write:**
  - a file that exists and differs from the template is left alone and reported;
  - a file identical to the template is recorded as `adopted`;
  - in `settings.local.json` and the exclude file, devproc only adds and removes its own entries.
- **Git:** in a git work tree, devproc paths not already ignored are added to the local exclude file, each under a marker line. If the file's last line has no newline, one is added first (and removed again on uninstall); a pair already there is not added twice; an exclude file devproc created is removed once empty. devproc 0.2.0 added no newline, so its marker could join the project's last pattern and break it: install, migration and uninstall split it off again. git runs in the C locale, without the caller's `GIT_DIR`, `GIT_WORK_TREE` or other variables naming a repository, so it always reads the target's own repository. The exclude file is never committed. The snapshot lives under `.claude/devproc/`, so it is ignored too.
- **Idempotent:** a second install sees the manifest and changes nothing. Without a manifest, install refuses while a killed run's leftovers are there (a snapshot under `dist/`, the registered marketplace, enabled devproc plugin entries), and prints the command that removes each. Only devproc 0.2.1 or older leaves these: 0.2.2 writes its manifest first. devproc's own marked exclude lines found there (kept by an uninstall because files stayed, or left by a killed run) are taken back into the new install's record.
- **No rollback, as dpkg** (since 0.2.2; 0.2.1 rolled back, which no handler can do after a SIGKILL or a power cut). Every step is safe to run again, and:
  - **The manifest is written first.** The first step of install, upgrade, repair and uninstall writes it with `pending` (`install`, `upgrade to <version>`, `repair`, `migrate`, `uninstall`) and every record the run needs, computed before anything runs: each seeded file with the template's hash and `was`, its hash before the run (none for a new file); the folders it will create; the exclude lines it will add; whether `settings.local.json` existed, and its exact bytes; and, for upgrade and repair, `previous`, the install as it was. The last work step, after the post-check, rewrites it without them.
  - **A run that stops part-way** (a failed step, an interrupt, a SIGKILL, a power cut) leaves that manifest. Rerunning install, upgrade or repair finishes it, from `previous`, to the source's version; uninstall removes it, whatever was done. While a run is pending, a seeded file counts as devproc's at its recorded hash or at `was`. `--status` reports the unfinished run (exit 4). Install refuses while an uninstall is unfinished.
  - **Every file write is atomic:** a temp file in the same folder, fsynced, renamed over the file, then the folder fsynced (rename(2), fsync(2)). A kill leaves the old bytes or the new ones; a temp file a kill left is removed by the next run, and reported by status. A snapshot is copied into `<dist>.partial`, fsynced, and renamed.
  - **Interrupts** (Ctrl-C, SIGTERM, SIGHUP; SIGHUP is left alone under `nohup`): the first stops the run before its next step (exit 3); a second (closing a terminal can send SIGHUP twice) stops it at once. A running `claude` or `git` call is never killed by an interrupt. Each runs in a session of its own, so the terminal's signals reach devproc only, with its output going to temp files rather than pipes: when devproc stops at once, the call finishes by itself instead of dying mid-write of the plugin registry every directory shares. Output to a terminal that is gone is dropped, so it cannot stop a run.
  - **Tidying:** steps after the final manifest only tidy up (snapshots moved aside or older than the previous one, old records, a dead registration). A failure or an interrupt there keeps the work and lists the steps not done.
- **Refused before writing anything** (install, upgrade, repair): a symlink at a devproc folder, seeded file, the manifest or `settings.local.json`; a tracked `settings.local.json`; a linked worktree whose main checkout git cannot name (bare or `--separate-git-dir`); any git error other than "not a git repository" (such as dubious ownership), which would otherwise silently drop the exclude lines and the top-level check. Uninstall and status refuse a symlinked devproc folder, manifest or `settings.local.json` too, when a manifest is found through it: that is another directory's install.
- **One run at a time:** `--apply` holds an exclusive lock on the target directory itself (no lock file is written); a second run is refused. It then takes a lock on Claude Code's config folder (`CLAUDE_CONFIG_DIR`, else `~/.claude`), whose plugin registry every directory shares: a run in another directory waits for it, up to 300 s, saying so; an interrupt ends the wait, with nothing changed. Each `claude` CLI call times out after 300 s (`DEVPROC_CLI_TIMEOUT`, 1 to 86400); a call that times out is killed with its process group, and a grandchild that left the group cannot keep devproc waiting.
- **Exit codes:** 0 done (status: healthy); 1 refused, nothing changed (status: not installed); 2 usage; 3 stopped part-way: a failed step, an interrupt or an unexpected error (rerun the same command to finish, or uninstall); 4 status found problems (a git error while checking included). git runs in the C locale, since devproc reads its messages. A broken or incomplete manifest, or a missing `git` or `claude`, gives a one-line message, not a traceback.
- **Post-check:**
  - the plugin is enabled in `settings.local.json` and in `claude plugin list --json` for this directory, at the expected version;
  - `readFromFolder` is this directory's snapshot, and the marketplace is registered at it;
  - the snapshot's tree hash equals the source's (every path with its kind, symlink targets, exec bits and file bytes);
  - the self-test passes on the snapshot's guard.
- **Status** (`--status`) lists each file's state and checks the install: snapshot present and unchanged (its `marketplace.json` included), marketplace registered for this snapshot, plugin enabled, an install record for this directory without load errors, `.claude/devproc/` ignored by git (and `settings.local.json`, if devproc added its exclude line; a tracked one is reported with the command to untrack it), directory not moved or copied. The rules folder may be committed: a team can share it. Exit 0 healthy, 1 not installed, 4 problems (each printed with its fix). It also says when the source is at another version, or differs under the same version.
- **Upgrade:**
  - refused when the source differs from the installed code under the same version;
  - fixes every problem status shows, as repair does (repair needs a source at the installed version; upgrade works from a newer one);
  - updates seeded files that are still as seeded; keeps evolved and adopted files, and prints the `diff -u` command for evolved ones; seeds new templates;
  - for a new version: writes the new snapshot (or reuses a kept one whose hash matches), repoints this directory's marketplace to it, updates the marketplace and the plugin;
  - keeps the previous snapshot for sessions still running, and removes older ones; an upgrade to the same version removes none;
  - for a 0.2.0 directory: gives it its own snapshot and marketplace, uninstalls `devproc@devproc` there and in its subdirectories (a session started in a subdirectory adds a record of its own; a nested folder with its own devproc install keeps its records), and leaves the shared `devproc` marketplace (removed from the registry only when no other install uses it). Refused while `devproc@devproc` is enabled here but the project had it before devproc installed (the 0.2.0 manifest records this).
- **Repair** (`--repair`) restores an install of the source's own version:
  - refused unless the source is at the installed version with the recorded `pluginHash`, so it restores exactly what was installed; a 0.2.0 install is sent to `--upgrade`;
  - rebuilds a missing or changed snapshot (the damaged one is moved aside, and removed after the post-check);
  - re-registers the marketplace at this directory's snapshot, and re-installs and re-enables the plugin;
  - a moved or copied project gets a marketplace named for its new place, and its record of the old plugin id is uninstalled. The original's marketplace is left alone, unless the original's folder no longer exists: then its dead registration is removed at the end;
  - re-adds missing devproc lines to this directory's own git exclude file;
  - stops like install, and a rerun finishes it. The tidying steps after the final manifest (removing snapshots moved aside, old snapshots, a dead registration) only warn on failure: the repaired install stays.
- **Upgrade and repair need the top of the main checkout**, like install. A directory that now sits inside another repo can only be uninstalled.
- **Uninstall** first marks the manifest `pending: uninstall`. It stops at a failed step, says so, and a rerun finishes it. It removes an install that did not finish as well: what that run had not done yet is simply not there. It:
  - removes this directory's marketplace, which also removes its plugin records, subdirectories included. A copy that was never repaired leaves the original's marketplace registered and uninstalls only its own records;
  - removes devproc's keys from `settings.local.json`, and everything under `.claude/devproc/dist/` (a stray symlink is removed, never followed);
  - removes seeded files unchanged since install;
  - edits this directory's own git exclude file, never the path recorded at install (a copy carries its `.git` with it, so the recorded path can be the original's). It removes lines only for paths where no file stays behind, so kept task data and evidence stay ignored. A `settings.local.json` the project had before install loses its exclude line, as before install;
  - restores a `settings.local.json` that existed before install to its exact bytes, if the project did not change it since; removes it only if devproc created it and it is now empty;
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

## 6. Test evidence (2026-10-27, versions 0.2.2 and 0.2.3)

0.2.3 changes only the browser bullet of `30-work.md`: an MCP browser's process is ended too, not only its pages. On 0.2.3, the guard self-test (91/91), the e2e (440/440) and the kill sweep (190/190) were rerun and pass. The installer mutants and the reviews below ran on 0.2.2; 0.2.3 keeps its installer code unchanged.

- **Live logged-in sessions of the browser rule (0.2.3, Claude Code 2.1.311):** two `claude -p` runs in a throwaway directory with devproc installed. The prompts asked only for a screenshot of a page and its title, nothing about closing the browser.
  - With the chrome-devtools MCP: the session closed its page, ended the browser process its MCP server had started, and checked that no Chrome was left under it before reporting. Its Chrome exited 11 s before the session ended. The machine also has a user-level instruction to close MCP browsers, which the session cited, so this run does not isolate the rule.
  - With a headless Chrome shell script: the script used its own temporary profile, ended Chrome by that profile path in a `trap` on exit and removed the profile; the session then checked with `pgrep` that nothing was left before reporting. No user-level instruction covers this case.
  - After uninstall, the real `settings.json`, `installed_plugins.json` and `known_marketplaces.json` were byte-identical to before.
- **`tests/selftest.py`:** 91/91 cases pass on `plugin/scripts/guard.py` (guard code unchanged from 0.2.1 apart from its version).
- **`tests/installer-e2e.sh`:** 440/440 checks pass, set up as for 0.2.1 below. New for 0.2.2: every former rollback check is now "exit 3, then a rerun finishes it" and "exit 3, then uninstall leaves the directory and registry as before"; kills (SIGKILL failpoints) between steps and inside file writes; first and second interrupts; a `claude` call left running holding the lock; worktree copies, repositories sharing one linked exclude file, a symlinked exclude file, a non-UTF-8 exclude line, a Latin-1 locale.
- **`tests/installer-killsweep.sh` (new):** 190/190 checks pass. Install, upgrade and uninstall are each killed with SIGKILL before every step; each time the project's own settings and exclude patterns are intact, a rerun finishes the run, and uninstall then leaves the directory byte-identical to before with no registry record left.
- **Installer mutants:** 106/106 caught, each by the e2e check written for it (the run stops at its first failed check, and that check must be the mutant's expected one). In the last full run 98 were caught at once; 6 were caught by an earlier check than the one named and were retargeted, and 2 (the kept marker naming its directory, and a reinstall taking its kept lines back) passed every check, so P39 got a check for each; all 8 were then caught. The 0.2.1 rollback mutants were removed with the code they tested.
- **Fresh-context reviews of the 0.2.2 installer:** eight, with the stopping rule set before the first: stop when a review finds nothing that can lose user data, leave a state neither a rerun nor an uninstall can finish, or touch another directory or the real config. Reviews 1 to 7 found 22 issues, all fixed, each with an e2e check and a mutant. Review 8 found one low-plausibility case, accepted as a known limit: in repositories sharing one exclude file (a linked file, or a 0.2.0/0.2.1 main checkout with a worktree copy), a line a 0.2.0 or 0.2.1 uninstall kept for task data left behind is adopted by a 0.2.2 install elsewhere and removed by its uninstall; that task data is then no longer git-ignored. Check with `git status`; add `/.claude/devproc/` back by hand.
- **Not repeated since 0.2.1:** the live push test below (a fake-key push denied by the hook). The guard code is unchanged since 0.2.1 apart from its version.

### Version 0.2.1 (2026-10-25)

- **`tests/selftest.py`:**
  - 91/91 cases pass on `plugin/scripts/guard.py`;
  - it fails on `tests/mutant_noop.py` (exit 1, as required);
  - on the first guard 0.1.0, 22 of the 84 cases of the time failed, each reproducing a gap from the fresh-context review.
- **`tests/installer-e2e.sh`:** 331/331 checks pass, with a sandbox `CLAUDE_CONFIG_DIR`, a hermetic git config (no global config or global ignore file), a fake `$HOME` for the home refusal, and throwaway targets. What a session loads is read from a real session's debug log: in the sandbox the session stops at login, after Claude Code has loaded its plugins. The 0.2.0-migration checks use the real 0.2.0 release (`OLD_SRC`, or the `v0.2.0` tag). Both copies pass: the internal one with `OLD_SRC`, the public one from its tag.
- **Installer mutants (`tests/installer-mutants.py`):** 78 copies of the installer, each with one safeguard switched off. Every one is caught by the very e2e check written for it: the run stops at its first failed check, and that check must be the mutant's expected one, so a flaky or unrelated failure does not count. A mutant whose file does not parse is rejected, not counted. Mutants that first passed every check, and got a check of their own: the registry check ("marketplace name taken over"), the original-folder-gone check (a copy whose original lost its snapshot), and the failed step's own undo (a CLI step that does its work, then fails).
- **Bugs found before release, all fixed:**
  - by the tests: going back to the kept previous version aborted with "already exists"; five registry checks ran a shell function inside `bash -c`, where it does not exist, so they passed whatever happened; one mutant's replacement did not parse, so it was "caught" for nothing;
  - by eleven fresh-context reviews of 0.2.1, among them: uninstalling a copy edited the original's git exclude file; appending to an exclude file without a final newline broke the project's last pattern; repair and upgrade dead-locked once the source version moved on; a rollback did not restore a pre-existing `settings.local.json`, or a snapshot's file modes, or ran no undo for the failing step's own partial work; a copy's repair could remove the original's live registration; a tracked `settings.local.json` would have carried absolute paths into commits; the docs told teams to commit the rules folder, which status then reported as a problem forever; any git error was taken as "not a repo"; SIGHUP during a rollback, with the terminal gone, stopped it; a 0.2.0 marker joined to the project's last pattern was never cleaned up; a second SIGHUP (closing a terminal can send two) cut a rollback short, and an interrupt killed a running `claude` call; uninstall followed a symlinked `.claude` into another directory's install; a `GIT_DIR` set by the caller pointed git at another repository; after a timeout, devproc waited without limit on a grandchild holding the output; an interrupt during tidying rolled back work whose old snapshot tidying had already removed; an interrupt could cut a file rewrite between truncate and write.
- **Live logged-in session** (final 0.2.1 code, `claude -p`, real login, throwaway directory with a bare remote):
  - the debug log shows `Read hooks.json … <dir>/.claude/devproc/dist/0.2.1/plugin/hooks/hooks.json` and the hook command `python3 -I "<dir>/.claude/devproc/dist/0.2.1/plugin/scripts/guard.py"` for `devproc@devproc-<id>`;
  - a push of a commit with a planted AWS-key pattern was denied with "devproc (P8): push blocked … cfg.py:1 [aws-access-key-id]"; the remote has no refs;
  - after uninstall, the real `~/.claude/settings.json`, `installed_plugins.json` and `known_marketplaces.json` are byte-identical to before; Claude Code's cache copy carries `.orphaned_at`, for its 14-day cleanup.
- **Earlier live checks** (0.1.0 and 0.2.0 guard, `--plugin-dir`): `/usr/bin/git push` stopped with "Hook PreToolUse:Bash requires confirmation … devproc (P17)"; `bash -c 'git push origin HEAD:main'` with a planted key was denied.
- **Skill substitution:** a live session loading `/devproc:verify` received `${CLAUDE_PLUGIN_ROOT}` expanded to the plugin path.
- **Fresh-context reviews:** a first review of all docs against the code found 25 issues; 0.2.0 fixes the code issues, and the docs state the rest as limits. A second review before publishing found no leaks and about 20 doc and adoption gaps, all fixed. For 0.2.1, eleven more fresh-context reviews of the installer found 72 defects (the last found none) and a number of smaller findings (listed in part above); every one is fixed.
