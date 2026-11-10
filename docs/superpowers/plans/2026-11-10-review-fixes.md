# Review Fixes Plan (2026-11-10)

**Goal:** Fix every finding from the 2026-11-10 review of the engine and its GitHub repo.

**How to use this plan:** Each task is small and stands alone. Give one task to one builder. A task says which files to touch, what to change, and when it is done. Line numbers are from `main` at `75de41b`; if they moved, search for the quoted text.

## Rules for every task

- Do **not** write new tests and do **not** run smoke tests. If an existing test fails only because the behaviour was changed on purpose, update that test's expected value and nothing else.
- Touch only the files the task lists. No refactors, no renames, no extra cleanup.
- One commit per task, message `fix: <task title>` (or `docs:` / `chore:`).
- Tasks in the same batch touch different files and can run at the same time. Batches run in order.
- Follow `CLAUDE.md`: no host names, IPs, key paths or bot names in tracked files; never force-push; do not change sshd or the firewall.
- Tasks marked **OWNER** need the Owner's yes before they are built or run.

## Decisions already made (do not reopen)

| Topic | Decision |
|---|---|
| Local branch `integrate/review-fixes` | Do not merge (it conflicts in 7 files). Redo only the configurable account and time zone (Task 20), then delete the branch (Task 30). |
| "ok" run with no progress | Keep burning an attempt (it stops endless reruns). Fix the docstring only. |
| Always-on Admin inbox | The dispatcher types a `[system]` line into the tmux session; Admin reads `cb inbox` and closes messages with `cb message done`. |
| Telegram as main mode | Accept it. Update the spec to say so (Task 27). |
| Rollover summary | Keep the engine-built summary. Fix the prompt wording (Task 12). |

---

## Batch 1 — must land before the next server update

### Task 1: Transient unit name
**Files:** `engine/claudebot/runner.py:123`
**Change:** In `unit_name_for`, return `claudebot-run-{N}.service` instead of `.scope`. `systemd-run` here runs in service mode, which rejects a `.scope` name.
**Done when:** no `.scope` string is left in `runner.py`.

### Task 2: Memory limit wrapper for interactive Admin
**Files:** `engine/claudebot/chat.py:405-422` (`memory_wrap`)
**Change:**
- When the user manager is available (`use_user` is true), build: `systemd-run --user --scope --collect -p MemoryMax=… -p MemorySwapMax=0 -- <cmd>`. Use `--scope`, not `--pty`. Scope mode keeps the caller's environment, working directory, terminal and pid.
- When it is not available, return the command unchanged (no system-mode `systemd-run`, which needs a password prompt).
- Update the docstring to match.
**Done when:** `--pty` no longer appears in `memory_wrap`.

### Task 3: MCP secrets in chat and always-on
**Files:** `engine/claudebot/chat.py:502`
**Change:** Call `runner._mcp_text(cfg, bot, conn)` (pass the open connection, as `runner.py:344` does).
**Done when:** both callers of `_mcp_text` pass `conn`.

---

## Batch 2 — logic bugs (one builder per file group)

### Task 4: Stalled items wake again (dispatcher)
**Files:** `engine/claudebot/dispatcher.py:531-591`
**Change:**
1. In `_wake_assigned`, count stall reverts for the item: `SELECT COUNT(*) FROM activity WHERE kind='item.stall_revert' AND ref=?`. When the count is above 0, append `:s{count}` to the dedupe key. Leave the key unchanged when the count is 0 (so existing items do not re-wake after the update).
2. When the count is 3 or more, do not wake the owner. Send Admin one message instead with `_system_send`, subject `Item {id} keeps stalling`, dedupe key `stall-limit:{id}`.
3. In `_stall_doing`, remove the lane filter `AND COALESCE(lane,'') IN ('','normal','heavy')` so an open chat or live run also counts as a live run.
**Done when:** a reverted item gets a new wake key, and Admin's own `doing` items are not reverted while Admin has an open run.

### Task 5: Dispatcher small fixes
**Files:** `engine/claudebot/dispatcher.py`
**Change:**
1. `finish` docstring (line 281): replace "(no attempt burned)" with "(this uses one attempt; after `retry_attempts` the message fails)".
2. Line 182: delete the stale comment about `-p` being one argument.
3. Tick step failures (line 474): keep a dict `self._step_errors` of step name → last error text. Write the `tick.step_failed` row only when the text for that step changed. Clear the entry when the step succeeds.
4. `_wake_admin` (line 629): delete the two lines `if self._live_up(): return`. The wake is then queued for Admin and reaches the live session through Task 7.
5. `_surfaced`: load it from `meta` key `admin.surfaced` (JSON list of ids) on first use and save it there whenever it changes, so a restart does not re-send the same notice.
**Done when:** all five edits are in.

### Task 6: Reassign resets the failure count
**Files:** `engine/claudebot/items.py` (the function that records `item.assign`, near line 126)
**Change:** In the same transaction as the assign, run `DELETE FROM meta WHERE key=?` with `"task.fail." + item_id`. Do not import `tasks` (circular import).
**Done when:** assigning an item removes its `task.fail.<id>` row.

### Task 7: Always-on Admin receives its inbox
**Files:** `engine/claudebot/admin_live.py`, `engine/claudebot/requests.py`, `engine/templates/admin/PERSONA.md`
**Change:**
1. `admin_live.surface` (line 285): first try `tmux send-keys -t admin-live -l "[system] " + one-line text`, then `tmux send-keys -t admin-live Enter`. Collapse newlines in the text to `; `. Only if tmux fails, fall back to `notify_telegram`. Return `None`.
2. Delete `write_alert` and `alerts_dir` from `admin_live.py` and every call to them.
3. `requests.py` prompt hook, in the plain-text branch only (`entries is None`, line 280): when the text starts with `[system] `, do not call `capture` and do not close questions. Do not apply this to Telegram entries.
4. `PERSONA.md`: add two sentences. "A line that starts with `[system]` comes from the dispatcher, not the Owner. Run `cb inbox`, handle each message, then close it with `cb message done ID`."
**Done when:** `surface` types into tmux, no alert file is written, and the hook ignores `[system]` lines typed in the terminal.

### Task 8: Close, retry and validate messages
**Files:** `engine/claudebot/messages.py`
**Change:**
1. New command `cb message done ID [ID…]`: sets `status='done'`, `done_at=now` for messages that are `queued`, `claimed` or `failed`. Allowed when the actor is the recipient, `admin` or `cli`. Records activity `message.done`.
2. New command `cb message retry ID`: for a `failed` message, sets `status='queued'`, `attempts=0`, `lease_until=NULL`, `done_at=NULL`. Allowed for `admin` or `cli`. Records activity `message.retry`.
3. `cb send --kind`: add `choices=("note", "question", "answer")`. `--priority`: add `choices=("normal", "urgent")`. Before adding, search the templates and code for `cb send --kind` values in use and include any that are missing.
4. Delete the unused `RESERVED_SENDERS` (line 19).
5. Hop chain (line 86-94): add `AND from_bot != 'system'` to the item lookup, and skip the lookup when `frm` is `system`. System wakes then no longer count as hops.
**Done when:** both commands exist and `cb send --kind wake` is refused.

### Task 9: Decisions survive archiving, and a session cannot be reused
**Files:** `engine/claudebot/db.py`, `engine/claudebot/items.py:181-215`, and the decision-recording function in `items.py`
**Change:**
1. Add column `decisions.owner_session INTEGER DEFAULT 0` using the existing migration style in `db.py`. In the same migration, set it to 1 for every existing decision whose `session` passes today's `owner_session_ok`.
2. When a decision is recorded, check now and store the result: `owner_session=1` only if the session passes the check **and** the caller's own `CB_SESSION_ID` equals that session (or the actor is `cli` with no `CB_SESSION_ID`). Otherwise refuse with `PermissionError`.
3. `require_owner_decision`: accept when `owner_session == 1`; stop looking the session up in `runs`.
**Done when:** a decision stays valid after its run row is archived, and a run cannot record a decision under another session's id.

### Task 10: Bots, runner and routine paths
**Files:** `engine/claudebot/bots.py`, `engine/claudebot/runner.py:72`, `engine/claudebot/routines.py:228`
**Change:**
1. `bots.update` (line 242): when `org` is in `fields`, apply the same reserved-org check that `create` uses.
2. `bots.py:374`: when `rules.md` is larger than the cap, print a warning to stderr naming the size and the cap (keep cutting it).
3. `runner._resolve`: refuse absolute names and names with `..`; raise `ValueError`. Resolve only under `cfg.home / kind`.
4. `runner.build_command`: delete the unused `agents` and `task` parameters and the code that uses them. Fix any caller.
5. `routines._run_check`: refuse a check file that is not under `cfg.home / "checks"`. Update the `--check` help text to say so.
**Done when:** all five edits are in.

### Task 11: Doctor and config checks
**Files:** `engine/claudebot/doctor.py`, `engine/claudebot/config.py`, `engine/claudebot/gitlog.py:15`
**Change:**
1. `doctor.py`: add `"workers"` to `NO_WAKE`. Skip the `workers` finding entirely when Admin is the only thinker without a worker.
2. `doctor.py`: fix the docstring that says "once a day" to "every 7 days".
3. `config.py` `__post_init__`: if `lease_seconds < run_seconds + 150`, raise `ValueError` naming both values.
4. `gitlog.py`: in the company-home ignore list, remove `.superpowers/` and add `.alerts/`.
**Done when:** all four edits are in.

### Task 12: Chat and rotation leftovers
**Files:** `engine/claudebot/chat.py`, `engine/claudebot/requests.py:306`, `engine/claudebot/admin_live.py:146`
**Change:**
1. Delete `CB_ROLLOVER_TOKEN` (chat.py:42) and anything that sets it.
2. Delete the retired `cb hook stop` command: `cmd_hook_stop` in chat.py, `_cmd_hook_stop` in requests.py and its parser entry. Also remove it from any settings the engine writes.
3. In the rollover prompt text (chat.py:375 and admin_live.py:146), replace "Your own summary" with "Summary built by the engine from the record (it has no conversation text)".
4. Failed messages in the brief (chat.py:191): show only those that failed in the last 7 days, and add the line "Clear one with `cb message done ID` or rerun it with `cb message retry ID`."
**Done when:** all four edits are in.

### Task 13: Archive and growth
**Files:** `engine/claudebot/archive.py`
**Change:** In the daily archive run, also delete:
- `usage` rows older than 180 days;
- `meta` rows with keys starting `task.finished.`, `usage.offset.` or `doctor.seen:` whose message, session or finding no longer exists;
- pre-update database copies older than 30 days (keep the newest 3 whatever their age).
Also include `failed` messages older than the cutoff in the plan (line 488), the same way `done` messages are.
Do not delete from `activity` (it is append-only by design).
**Done when:** the four clean-ups run inside the existing daily archive step.

---

## Batch 3 — security hardening

### Task 14: Secret masking and Telegram token
**Files:** `engine/claudebot/store.py`, `engine/claudebot/admin_live.py:119`
**Change:**
1. `cb secret run` masking: also mask the base64 and hex forms of each secret value.
2. Telegram token file: create it with mode `600`, and call `scrub_token` in `cleanup()` so it is removed whenever always-on Admin stops (check that it already does; add it if not).
**Done when:** both edits are in.

### Task 15: Telegram hook blocks unknown senders
**Files:** `engine/claudebot/requests.py:287`
**Change:** When every Telegram entry in the prompt comes from a sender that is not on the allow list, print the hook's "block" JSON (same shape as the acknowledgement block at line 275) with the reason "Message from a sender that is not on the allow list." and return.
**Done when:** a prompt made only of denied senders is blocked, not just unrecorded.

### Task 16: Force-push guard
**Files:** `engine/claudebot/backup.py:97`
**Change:** In `_assert_no_force`, also refuse any argument that starts with `+` (a forced refspec) and `--mirror`.
**Done when:** `+main:main` is refused.

### Task 17: Backup scan before first store
**Files:** `engine/claudebot/backup.py`
**Change:** Before the backup commit, scan the dump for these token shapes and stop the backup with a clear error naming the table and row id (not the value) if any match: `sk-ant-`, `ghp_`, `github_pat_`, `xox[bap]-`, `AKIA[0-9A-Z]{16}`, Telegram bot tokens (`\d{8,10}:[A-Za-z0-9_-]{35}`), and `-----BEGIN [A-Z ]*PRIVATE KEY-----`.
**Done when:** a dump containing one of these shapes is not pushed.

### Task 18: Signed release tags — **OWNER**
**Files:** `engine/update.sh:127`, `UPDATING.md`
**Change:** Before checking out a tag, run `git verify-tag "$TAG"`. If it fails, stop with an error unless `ALLOW_UNSIGNED=1` is set. Document in `UPDATING.md` how to import the Owner's signing key once.
**Owner step:** create a signing key and sign future tags.
**Done when:** an unsigned tag is refused by default.

### Task 19: Bots without root — **OWNER**
**Files:** `vps-bootstrap.sh:69`, `engine/install.sh`, `engine/claudebot/runner.py:140`, `docs/deploy.md`
**Change:**
1. Bootstrap creates a second account `cbbot` with no sudo, in a shared group `claudebot` with the Owner account.
2. `install.sh` makes the company home group-owned by `claudebot`, group-writable, with the setgid bit on directories. `.secrets/` stays owner-only.
3. Background runs use `--uid=cbbot --gid=claudebot` and `HOME=/home/cbbot`.
4. Replace `NOPASSWD:ALL` with a rule that lets the Owner account run only `systemd-run` and `systemctl stop claudebot-run-*`, `systemctl restart claudebot-*`.
5. Describe the change and the move steps for an existing server in `docs/deploy.md`.
**Warning:** this changes sudoers. Keep one SSH session open and test a second login before closing it.
**Done when:** a background bot run cannot use sudo or read `.secrets/`.

---

## Batch 4 — one install for any account

### Task 20: Configurable account, home and time zone
**Files:** `engine/claudebot/config.py`, `runner.py:22,141`, `db.py:82`, `routines.py:29`, `engine/install.sh`, `engine/update.sh`, `engine/systemd/*.service`, `vps-bootstrap.sh`
**Change:**
1. Add to `Config`: `run_user: str` (default: the current user name), `timezone: str = "America/Chicago"`. Change `claude_bin` default to `~/.local/bin/claude` of the current user.
2. Replace `CLAUDE_HOME`, `--uid=claude`, and both `America/Chicago` constants with the config values.
3. `install.sh` and `update.sh`: drop the "run as user claude" check; refuse only root. Write `User=` in the unit files from the installing user (replace a `@USER@` placeholder with `sed` on install).
4. Replace "he/his" for the Owner with "they/their" in engine text and templates.
**Done when:** `grep -rn "/home/claude\|uid=claude\|User=claude" engine` returns nothing.

### Task 21: Update script
**Files:** `engine/update.sh`, `engine/claudebot/dispatcher.py` (CLI part only), `UPDATING.md`
**Change:**
1. New command `cb dispatcher idle`: exit 0 when no run has `ended_at IS NULL` outside the chat and live lanes, else exit 1.
2. `update.sh` before the restart (line 209): poll `cb dispatcher idle` every 10 seconds for up to 30 minutes, then restart anyway and print a warning.
3. `update.sh`: copy changed unit files from `engine/systemd/` to `/etc/systemd/system/` (same commands as `install.sh`) and run `systemctl daemon-reload`.
4. `update.sh`: refresh the engine-owned templates (`rules.md`, skills) in the company home; never overwrite a bot's `SOUL.md` or `PERSONA.md`.
5. Remove the matching "update does not do this" notes from `UPDATING.md`.
**Done when:** all five edits are in. Run this task after Task 5 (same file, different part).

### Task 22: Skill links
**Files:** `engine/claudebot/bots.py`, `engine/claudebot/doctor.py`
**Change:** New command `cb bot relink` that recreates every bot's skill symlinks to point at the current engine checkout. Doctor reports broken skill links with the fix "run `cb bot relink`".
**Done when:** the command exists and doctor names it. Run after Tasks 10 and 11.

### Task 23: Record sudo use
**Files:** `engine/templates/rules.md`, `engine/claudebot/activity.py` (CLI part)
**Change:** New command `cb log sudo "what and why"` that records activity kind `sudo`. Add one rule line to `rules.md`: "Before any `sudo` command, record it with `cb log sudo`." Keep `rules.md` under 4 KB.
**Done when:** the command exists and the rule is in the template.

---

## Batch 5 — docs, templates and repo hygiene

### Task 24: Stale template and unused statuses
**Files:** `engine/templates/skills/cb-basics/SKILL.md`, `engine/claudebot/items.py`
**Change:**
1. In the skill, replace the "not wired into MCP yet" sentence with: "Write a token in an MCP definition as `${secret:NAME}`; the engine fills it in when the run starts."
2. Remove the item statuses `pending`, `feature`, `slice` and `finding` from the allowed list if `grep` shows no other use. Leave any that are used.
**Done when:** both edits are in.

### Task 25: README and secret-store wording
**Files:** `README.md`
**Change:** Replace the "never passed through a model session" claim with what is true: secrets are encrypted at rest with a key on the same server; they protect against backups and accidental display, not against a bot that decides to read them. Add one line that the Telegram token is on disk in plain text while always-on Admin runs. Remove the claim that GitHub has only `main`.
**Done when:** the three wording changes are in.

### Task 26: Remove host details from docs
**Files:** `docs/superpowers/plans/acceptance-notes.md`, `docs/superpowers/specs/2026-11-08-claudebot-engine-design.md`, `docs/superpowers/plans/2026-11-09-claudebot-cost-control.md`, other files under `docs/superpowers/`
**Change:** Replace the server host name, deploy-key file names, backup organisation and repository names, the SSH alias, and the two company names with neutral placeholders (`<server>`, `<deploy-key>`, `<backup-repo>`, `<ssh-alias>`, `<company>`). Do not rewrite git history.
**Done when:** a case-insensitive search of `docs/` for the old server hostname returns nothing, and the other company and server names are gone.

### Task 27: Bring specs and changelog in line with the code
**Files:** `docs/superpowers/specs/2026-11-08-claudebot-engine-design.md`, `CHANGELOG.md`, `docs/hygiene-leftovers.md`, `docs/branch-cleanup.md`, `docs/ops-checklist-review-backlog.md`
**Change:**
1. Spec: add a short "Changes since this design" section: Telegram always-on Admin is the main mode; the rollover summary is built by the engine; models follow tiers; sudo is recorded by `cb log sudo`.
2. `CHANGELOG.md`: change "escalated to the thinker" to "escalated to Admin". Add an entry for PRs 11–19 and one for this plan under a new version heading `0.0.2`.
3. Delete `docs/hygiene-leftovers.md` (its open items are done) and remove links to it.
4. `branch-cleanup.md` and the ops checklist: remove the claim that only `main` exists; say merged branches are deleted after each release.
**Done when:** all four edits are in.

### Task 28: Shared CLI helpers
**Files:** new `engine/claudebot/clihelp.py`, and the seven modules that define `_actor()` and `_run()`
**Change:** Move the two functions to `clihelp.py` as `actor()` and `run(fn)`. Replace each local copy with an import. Make `requests.py` import public names instead of private ones from `items.py` (rename those two or three functions without the leading underscore).
**Done when:** `grep -n "def _actor\|def _run(" engine/claudebot` returns nothing. Run this task last among code tasks (it touches many files).

### Task 29: CI
**Files:** `.github/workflows/*.yml`
**Change:** Add two steps to the existing job: `shellcheck engine/*.sh vps-bootstrap.sh` and `python -m pyflakes engine/claudebot`. Fix only what they flag as errors.
**Done when:** both steps are in the workflow.

### Task 30: Repository clean-up — **OWNER**
**No code.** Commands for the Owner (or a builder with the Owner's yes):
1. Add a `LICENSE` file (Owner picks the licence, or "All rights reserved").
2. Move `backups/*.tar.gz` out of the repo folder.
3. Delete the 9 merged branches on GitHub and turn on "Automatically delete head branches".
4. Delete the 11 stale local branches, including `integrate/review-fixes` (after Task 20 is merged).
5. Remove the local `.superpowers/` folder if no longer used.

### Task 31: Release
**Files:** `engine/claudebot/__init__.py`
**Change:** Set `__version__ = "0.0.2"`. After everything is merged to `main`, the Owner creates the annotated tag `v0.0.2` and a GitHub release, then runs `engine/update.sh` on the server.
**Done when:** the version string is `0.0.2`.

---

## After the release (Owner, on the server)

These cannot be checked on a Mac. Look at them once after `update.sh`:

- A bot run starts (Task 1).
- `cb chat` and always-on Admin start in the Admin bot folder (Task 2).
- The usage-limit message text still matches `runner.py:30`.

## Finding → task map

| Finding | Task | Finding | Task |
|---|---|---|---|
| A1 bots are root | 19 | B9 restart orphans runs | 21 |
| A2 self-declared identity | 19 (real fix), 9 (decisions) | B10 hop limit on busy items | 8 |
| A3 decision provenance | 9 | B11 fresh install never "all good" | 11 |
| A4 secret store | 14, 25 | B12 Admin items flip to todo | 4 |
| A5 backups carry tokens | 17 | B13 activity row every 5 s | 5 |
| A6 force-push guard | 16 | B14 lease vs run time | 11 |
| A7 unsigned tags | 18 | C1 unit name | 1 |
| A8 Telegram hook | 15 | C2 interactive wrapper | 2 |
| A9 check and plugin paths | 10 | D drift items | 7, 12, 20, 23, 27 |
| A10 host details in docs | 26 | E code leftovers | 5, 7, 8, 10, 11, 12, 24, 28 |
| B1 stalled item | 4 | E doc leftovers | 25, 27 |
| B2 reassign after failures | 6 | E repo leftovers | 30, 31 |
| B3 decisions expire | 9 | F improvements | 13, 21, 22, 29 |
| B4 MCP secret in chat | 3 | F ack filter fails open | not planned: needs an Owner decision on which is worse, a dropped "yes" or a stray "ok" starting work |
| B5 Admin inbox | 5, 7, 8 | | |
| B6 reserved org | 10 | | |
| B7 no-progress attempt | 5 | | |
| B8 failed messages | 8, 12 | | |
