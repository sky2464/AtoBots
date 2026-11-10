# ClaudeBot cost control and tiers (v0.0.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut ClaudeBot's subscription burn with thinker/worker tiers, cache-stable prompts, engine-driven session rotation, a usage ledger and fewer Owner confirmation turns, then ship it as version 0.0.1 with an update guide.

**Architecture:** Additive SQLite changes (`bots.tier`, `bots.worker`, `usage` table) migrated in `db._migrate`. Routing and caps change in `dispatcher.py`/`runner.py`; new small modules `usage.py`, `rotation.py`, `tasks.py` hold new logic. Prompts/templates carry the "ask less" rules.

**Tech Stack:** Python 3 (stdlib only), SQLite, `unittest` (run: `python3 -m unittest discover -s tests`; baseline 274 tests, 15 skipped), systemd, Claude Code CLI.

**Spec:** `docs/superpowers/specs/2026-11-09-claudebot-cost-control-design.md` (read it first; the section "My calls" holds the decided details).

## Global Constraints

- Engine only: everything lives in `engine/`, `tests/`, `docs/`, and root `README.md`/`VERSION`/`CHANGELOG.md`/`UPDATING.md`.
- Standard library only; no new services; runs on Debian 13, 2 GB RAM.
- All 274 existing tests keep passing (15 skipped on the Mac for GnuPG). New tests use `unittest`, in the style of `tests/test_runner.py` (tempdir `Config`, `connect`, `init_db`).
- Schema changes are additive and repeatable (follow `db._migrate`).
- Never change `templates/rules.md` rules on publishing, spending, or the Owner's word.
- Bot models are never silently changed; mismatches are only logged/reported.
- Version string is `0.0.1`.

## Review Focus

- A bot with no `worker` set and an item assigned to it: owner is still woken (today's behavior) and "no worker set" is flagged, not stranded.
- `MEMORY.md` edited between two runs: stable prefix bytes unchanged, task packet carries the new text.
- Existing company database (no `tier`, `worker`, `usage` yet) opens, migrates, and existing bots get tier from model.
- An Owner-originated message to a capped thinker still starts a run.
- A bare "ok" / "yes" / thumbs up with an open Admin question is NOT dropped; with none open it is dropped.

---

### Task 1: Schema, tiers and worker field

**Files:**
- Modify: `engine/claudebot/db.py` (SCHEMA `bots` + new `usage` table, `_migrate`), `engine/claudebot/bots.py` (`Bot`, `_row_to_bot`, `create`, `update`, `_PLAIN_FIELDS`, CLI flags), `engine/claudebot/cli.py` (nothing to hard-code)
- Test: `tests/test_bots.py` (add), `tests/test_db.py` (add)

**Interfaces:**
- Produces: `Bot.tier: str` (`"thinker"|"worker"`) and `Bot.worker: str | None`; `bots.create(..., tier: str | None = None, worker: str | None = None)` (tier default: `"worker"` if model == `"haiku"` else `"thinker"`); `bots.TIERS = ("thinker","worker")`; `bots.model_mismatches(conn) -> list[tuple[str, str, str]]` returning `(bot, model, problem)` where a thinker on `haiku` or a worker not on `haiku` is a problem; table `usage(id INTEGER PRIMARY KEY, at TEXT, bot TEXT, model TEXT, lane TEXT, trigger TEXT, ref TEXT, input_tokens INT, cache_write INT, cache_read INT, output_tokens INT)`.
- `cb bot new/set` accept `--tier` and `--worker`.

- [ ] **Step 1: Write failing tests** — `test_migration_adds_tier_and_worker`: create a DB with the old `bots` schema (no new columns) containing one `model='haiku'` and one `model='opus'` bot, call `connect(cfg)`, assert tiers are `worker` and `thinker`, `usage` table exists. `test_create_defaults_tier_from_model`. `test_model_mismatches_flags_thinker_on_haiku_and_worker_on_sonnet`. `test_set_worker_must_be_existing_worker_bot` (setting `worker` to a thinker or unknown bot raises `ValueError`).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_bots tests.test_db -v`; expect the new tests to FAIL.
- [ ] **Step 3: Implement** the columns in SCHEMA and in `_migrate` (ALTER TABLE ... ADD COLUMN, guarded like the existing `source` migration; after adding `tier`, run one UPDATE setting it from `model`), the `usage` table in SCHEMA, the dataclass fields, create/update/CLI changes and `model_mismatches`.
- [ ] **Step 4: Run** the full suite; expect all PASS.
- [ ] **Step 5: Commit** `feat: bot tiers, worker field, usage table`.

### Task 2: Models by tier (levels never raise a model)

**Files:**
- Modify: `engine/claudebot/runner.py` (`LEVEL_MODEL`, `settings_for`), `engine/claudebot/dispatcher.py` (`startup`: log mismatches), `engine/claudebot/doctor.py` (a `models` check line)
- Test: `tests/test_runner.py`, `tests/test_dispatcher.py`, `tests/test_doctor.py` if one exists else in `test_dispatcher.py`

**Interfaces:**
- Consumes: `bots.model_mismatches(conn)` from Task 1.
- Produces: `settings_for(bot, msgs) -> (model, effort)` where `housekeeping` gives `"haiku"` only for thinkers (workers are already haiku), and `deep`/`heavy` keep the bot's own model (they still raise effort and lane). `LEVEL_MODEL = {"housekeeping": "haiku"}`.

- [ ] **Step 1: Tests** — `test_deep_level_keeps_sonnet_bot_on_sonnet`, `test_heavy_level_never_picks_opus`, `test_housekeeping_runs_on_haiku`, `test_startup_logs_model_mismatch` (activity kind `model.mismatch`, detail names bot).
- [ ] **Step 2: Run** them; expect FAIL.
- [ ] **Step 3: Implement** `settings_for`/`LEVEL_MODEL`, the startup activity record (once per bot per start), and a doctor line listing mismatches (or "models: ok").
- [ ] **Step 4: Run** the full suite; PASS.
- [ ] **Step 5: Commit** `feat: models follow the bot, not the level`.

### Task 3: Stable cache prefix

**Files:**
- Modify: `engine/claudebot/bots.py` (`identity_prompt`), `engine/claudebot/runner.py` (`task_packet`, `run`)
- Test: `tests/test_runner.py`, `tests/test_bots.py`

**Interfaces:**
- Produces: `identity_prompt(cfg, conn, name) -> str` now contains only Soul, Persona and Shared rules (no Memory). `task_packet` begins with a `## Memory` block (the bot's `MEMORY.md`, capped at `cfg.memory_cap_bytes`) and a `## Colleagues` block (name, role of the other active bots) before the messages. `run()` no longer passes `agents=` to `build_command` (the `--agents` payload changes whenever a bot is added; `build_command` keeps its optional `agents` parameter).

- [ ] **Step 1: Tests** — `test_prefix_bytes_identical_when_memory_changes` (build `identity_prompt` twice with `MEMORY.md` edited between: equal bytes); `test_task_packet_carries_memory_and_colleagues`; `test_run_command_has_no_agents_flag` (capture the command through the existing fake-sudo harness).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Keep byte order deterministic (no timestamps or counters in the prefix).
- [ ] **Step 4: Run** full suite; fix any existing test that asserted Memory inside the identity prompt by moving that assertion to the task packet.
- [ ] **Step 5: Commit** `feat: keep the cached prompt prefix stable`.

### Task 4: Usage ledger and `cb usage`

**Files:**
- Create: `engine/claudebot/usage.py`
- Modify: `engine/claudebot/runner.py` (record after each run), `engine/claudebot/cli.py` (`MODULES` add `usage`)
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces in `usage.py`:
  - `record(conn, *, bot, model, lane, trigger, ref, input_tokens, cache_write, cache_read, output_tokens, at=None) -> int`
  - `from_result(data: dict | None) -> dict` reading the claude JSON result: `usage.input_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`, `usage.output_tokens`, and the served model from the first key of `modelUsage` (None if absent).
  - `ingest_transcripts(cfg, conn, bot="admin") -> int`: reads new assistant lines (those with `message.usage`, not `isSidechain`) from `cfg.claude_projects_dir` transcripts of the bot's folder (directory name is the bot folder path with `/` replaced by `-`), remembering per-file byte offsets in `meta` key `usage.offset.<filename>`; records each as trigger `session`; returns the count.
  - `report(conn, since: str = "24h", by: str = "bot") -> str` (`by` one of `bot|model|trigger`): totals per group plus cache hit rate = cache_read / (input + cache_write + cache_read); ends with lines for runs whose model differs from the bot's configured model.
  - CLI: `cb usage [--since 24h|7d] [--by bot|model|trigger]`; before reporting it calls `ingest_transcripts`.
- `runner.run` calls `record(...)` once per run using `from_result(data)`; trigger is the first message's `kind` (or `"run"`); `ref` is the run id.

- [ ] **Step 1: Tests** — `test_from_result_reads_cache_fields_and_model`; `test_ingest_is_incremental` (second call with no new lines returns 0; appended line returns 1); `test_report_hit_rate` (known numbers give the expected percentage string); `test_report_flags_model_mismatch`; `test_run_records_usage` (fake-sudo run writes one `usage` row).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** the module and the runner hook (a failure to record must never fail a run: catch, log activity `usage.record_failed`).
- [ ] **Step 4: Run** full suite; PASS.
- [ ] **Step 5: Commit** `feat: usage ledger and cb usage`.

### Task 5: Worker routing and the task/result protocol

**Files:**
- Create: `engine/claudebot/tasks.py`
- Modify: `engine/claudebot/dispatcher.py` (`_wake_assigned`), `engine/claudebot/cli.py` (`MODULES` add `tasks`), `engine/claudebot/runner.py` (`task_packet` text for `task` kind)
- Test: `tests/test_tasks.py`, `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `Bot.worker`, `Bot.tier` (Task 1); `messages.send`.
- Produces in `tasks.py`:
  - `send_task(conn, frm, to, item_id, goal, inputs, outputs, check) -> int`: sends a `kind="task"` message to the worker; body is JSON with those keys; refuses (`ValueError`) if `to` is not a worker-tier bot. Counts against `hop_limit` (do not use `_NEVER_STOP`).
  - `finish_task(cfg, conn, message_id, status, note) -> int`: `status` `done|failed`. For `done`, every path in the task's `outputs` must exist (relative to `cfg.home`); if one is missing the status becomes `failed`. Sends a `kind="result"` message to the task's sender with the outcome. On `failed`, increments the item's failure count in `meta` key `task.fail.<item_id>`; at 2 failures sends one message to `admin` (if the sender is admin, activity kind `task.escalate` only) and stops sending further wakes for that item.
  - CLI: `cb task send --to W --item ID --goal G --input P... --output P... --check C` and `cb task finish ID --status done|failed --note N`.
- Dispatcher: `_wake_assigned` now wakes `Bot.worker` of the item's owner when the owner has one, with the item text as the body if no earlier `task` message exists for that item; if the owner has no worker it wakes the owner as today and records activity `worker.missing` once per bot per day. Result and failure messages wake the thinker via normal queueing.

- [ ] **Step 1: Tests** — `test_send_task_rejects_thinker_recipient`; `test_finish_done_requires_output_files`; `test_second_failure_escalates_once` (exactly one message to admin, third failure sends nothing); `test_wake_assigned_goes_to_worker`; `test_wake_assigned_falls_back_to_owner_without_worker_and_flags`.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full suite; PASS.
- [ ] **Step 5: Commit** `feat: workers run tasks, thinkers wake on results`.

### Task 6: Wake caps by tier, Owner bypass, batching

**Files:**
- Modify: `engine/claudebot/config.py` (new fields), `engine/claudebot/dispatcher.py` (`_wake_capped`, `_start_ready`)
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Config adds `wake_cap_opus: int = 3`, `wake_cap_sonnet: int = 6`, `wake_cap_haiku: int = 12`, `worker_batch_delay_seconds: int = 60` (all overridable in `config.json`; `wakes_per_bot_per_hour` stays as the fallback for unknown models).
- `_wake_capped(conn, bot_name, now, msgs)`: cap picked by the bot's model; returns False (not capped) when any message in `msgs` has `from_bot == "owner"` or priority urgent from `admin`. Workers use `worker_batch_delay_seconds` instead of `batch_delay_seconds` in `_start_ready`.

- [ ] **Step 1: Tests** — `test_opus_bot_capped_at_three_runs_per_hour`; `test_owner_message_bypasses_cap`; `test_worker_waits_sixty_seconds_to_batch` (two messages 10 s apart become one claim).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement** (update the existing caller of `_wake_capped` to pass the queued messages).
- [ ] **Step 4: Run** full suite; PASS.
- [ ] **Step 5: Commit** `feat: per-tier wake caps and worker batching`.

### Task 7: Engine-driven rotation

**Files:**
- Create: `engine/claudebot/rotation.py`
- Modify: `engine/claudebot/config.py` (`idle_rotate_tokens: int = 80000`, `cache_ttl_seconds: int = 3600`), `engine/claudebot/admin_live.py` and `engine/claudebot/chat.py` (read how `_one_session`/the wait loop and `rollover` work, then call the new check from the running loop), `engine/claudebot/chat.py` (`cmd_hook_stop`: stop blocking; return 0 always)
- Test: `tests/test_rotation.py`, `tests/test_chat.py`

**Interfaces:**
- Produces in `rotation.py`:
  - `should_rotate(tokens: int, idle_seconds: float, cfg) -> bool`: True when `tokens >= cfg.chat_rollover_tokens`, or when `tokens >= cfg.idle_rotate_tokens` and `idle_seconds > cfg.cache_ttl_seconds`.
  - `build_summary(cfg, conn, last_owner_messages: int = 5) -> str`: from the database only: open items (id, title, status, owner), the last N `owner_requests` texts, requests not yet handled, the last 5 decisions. No model call.
  - `idle_seconds(transcript_path) -> float` (now minus the file's mtime).
- Wiring: while the live session runs, a thread checks every 60 s: `tokens = chat.context_tokens(path)`; if `should_rotate` and no turn is in progress (transcript mtime older than 20 s), calls the existing `chat.rollover(cfg, conn, build_summary(...))`.

- 1-hour cache for the live Admin: look up (Claude Code docs via `claude --help` on the VPS or the docs site) the environment variable that selects the 1-hour prompt-cache TTL and set it in `Live.session_env` (`admin_live.py`) only; if no such switch exists, set `cfg.cache_ttl_seconds` default to 300 instead and say so in the commit message.

- [ ] **Step 1: Tests** — `test_rotates_at_hard_threshold`; `test_idle_rotation_needs_both_size_and_idle` (small and idle: no; large and fresh: no; large and idle: yes); `test_summary_lists_open_items_and_last_owner_messages`; `test_stop_hook_no_longer_blocks` (large transcript, hook prints nothing).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Keep the rollover file format unchanged so the existing restart path (`_read_rollover`, `compose`) works.
- [ ] **Step 4: Run** full suite; PASS.
- [ ] **Step 5: Commit** `feat: engine rotates sessions before the cache cliff`.

### Task 8: Fewer Owner questions and the acknowledgement filter

**Files:**
- Modify: `engine/claudebot/db.py` (table `owner_questions(id INTEGER PRIMARY KEY, asked_at TEXT, text TEXT, answered_at TEXT)`), `engine/claudebot/requests.py` or `engine/claudebot/chat.py` (where `cb hook prompt` lives), `engine/claudebot/admin_live.py` (`LIVE_PROMPT`), `engine/templates/admin/PERSONA.md`, `engine/templates/rules.md` (add only a new "Asking the Owner" section)
- Test: `tests/test_requests.py`, `tests/test_templates.py`

**Interfaces:**
- Produces: `requests.is_bare_ack(text: str) -> bool` (case-insensitive, whitespace trimmed; true for yes, y, ok, okay, k, thanks, thx, 👍, 👌, 🙏 and combinations of those with punctuation; false for anything with other words); CLI `cb question open "<text>"` (inserts a row) and `cb question close` (marks all open rows answered).
- `cb hook prompt`: if `is_bare_ack(text)` and there is no open `owner_questions` row, it records nothing and prints `{"decision": "block", "reason": "Acknowledgement ignored (nothing was asked)."}`; with an open question it behaves as today and closes the question once the message is saved.
- Prompt text (fixed copy): "Act without asking on reversible, low-risk work. Ask the Owner only for publishing, spending, signing up, commitments to other people, and real choices. Put every open question in one message and register it with `cb question open`. Never reply to a bare yes, ok or thumbs up that answers nothing; never ask 'should I proceed?' for work already requested."

- [ ] **Step 1: Tests** — `test_is_bare_ack_true_cases` and `test_is_bare_ack_false_cases` ("yes please publish it" is False); `test_hook_blocks_ack_without_open_question`; `test_hook_keeps_ack_when_question_open` (and closes it); `test_live_prompt_and_persona_contain_ask_rules`.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Before relying on the filter, the builder checks on the VPS (`ssh atoosa-vps`, read-only) that a Telegram channel event does pass through the `UserPromptSubmit` hook; if it does not, keep the code and the prompt rules, and say so in the commit message.
- [ ] **Step 4: Run** full suite; PASS.
- [ ] **Step 5: Commit** `feat: ask the Owner less; drop bare acknowledgements`.

### Task 9: Version 0.0.1, update script and guide

**Files:**
- Create: `VERSION` (content `0.0.1`), `CHANGELOG.md`, `UPDATING.md`, `engine/update.sh`
- Modify: `engine/claudebot/cli.py` (`cb version` and `cb --version`), `README.md` (link to UPDATING.md; remove "Status: early and private" wording only if the Owner has made the repo public: leave it unchanged), `engine/claudebot/doctor.py` (print the version)
- Test: `tests/test_version.py`

**Interfaces:**
- `cli.version() -> str` reads the repo-root `VERSION` file, stripped.
- `engine/update.sh [--tag vX.Y.Z]` (run as user `claude`): refuses if the git tree is dirty; copies `$CLAUDEBOT_HOME/claudebot.db` to `$CLAUDEBOT_HOME/backups/pre-update-<version>-<date>.db` (create the folder); `git fetch --tags`; checks out the given tag, or the newest `v*` tag when none is given; runs `cb init --company <existing company from meta>` (applies migrations; idempotent); restarts `claudebot-dispatcher` and `claudebot-admin` with `sudo systemctl restart`; runs `cb doctor`; prints the old and new version. On any failure it prints how to go back (`git checkout <old tag>` and restore the backup) and exits non-zero. Use `set -euo pipefail` and the path rules of `install.sh`.
- `UPDATING.md`: who it is for (customers who installed from GitHub), how to see the current version (`cb version`), how to update (`engine/update.sh`), what it backs up and where, how to roll back, what to do if `cb doctor` reports problems, and a note that each release lists changes and any manual step in `CHANGELOG.md`. `CHANGELOG.md`: `## 0.0.1 - 2026-11-09` listing the changes of Tasks 1-8 in plain words.

- [ ] **Step 1: Tests** — `test_version_file_matches_cli` (`cb version` prints `0.0.1`); `test_update_script_has_strict_mode_and_backup_step` (read the script text; assert `set -euo pipefail`, `pre-update`, `git fetch --tags`).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Run `shellcheck engine/update.sh` if installed.
- [ ] **Step 4: Run** the full suite; PASS.
- [ ] **Step 5: Commit** `feat: version 0.0.1, update script and guide`.

### Task 10: Ship (done by the coordinator, not a builder)

- [ ] Run the full suite; whole-branch review by one fresh reviewer.
- [ ] Push `plan/claudebot-engine` work to a feature branch, open a PR to `main`, merge after the Owner's word, tag `v0.0.1`, create the GitHub release with the changelog text.
- [ ] Do NOT update the live VPS in this task; offer the Owner `engine/update.sh` on it as the next step.
