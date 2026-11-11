# Spec B — Dispatcher and backup correctness

Fixes findings L1, L2, L5, L6, L7, L10, L14, L16 in
`docs/review/2026-11-10-review-findings.md`. Target release: 0.0.3.

## Goal

Stop the two failures that cost money or lose backups without anyone seeing
it, and clean up the wake logic around them.

## Rules for the builder

- Python standard library only. One task = one commit, in order.
- Each task changes the named test file. Run
  `python3 -m unittest discover -s tests` before each commit. No manual or
  server testing is part of this spec.
- Tests use the injected `clock`, `run_fn` and `mem_fn` of `Dispatcher`; never
  sleep in a test.
- Do not write any company, product, person or server name anywhere.

## Task B1 — A message with no progress uses an attempt (L1)

File: `engine/atobots/dispatcher.py`. Tests: `tests/test_dispatcher.py`.

Now: `finish()` calls `self._release(conn, pending, now, usage_limit=True)`
for messages left without progress after an `ok` run. That flag gives the
attempt back, so the message is retried without end.

Change:

1. Call `self._release(conn, pending, now)` (no flag). The attempt taken in
   `claim()` then counts, and after `cfg.retry_attempts` the message becomes
   `failed` and Admin gets the existing "Stuck" note.
2. In the "Stuck" note body, when the last run ended `ok`, say: "The bot's
   runs ended normally but it never replied, finished the task or updated the
   item."  To know this, pass a `no_progress=True` keyword from `finish()`
   through `_release()` to `_tell_stuck()`.

Done when: a new test runs the same question through
`retry_attempts` ok-runs with no reply and asserts status `failed`,
`attempts == retry_attempts`, and exactly one `stuck` message to Admin. Extend
`test_bare_ok_without_progress_does_not_complete_question` to assert
`attempts == 1` after the first requeue.

## Task B2 — The dump scan must not block backups on a mention (L2)

File: `engine/atobots/backup.py`. Tests: `tests/test_backup.py`.

Now: `_DUMP_SECRET_PATTERNS` matches bare prefixes such as `sk-ant-` and
`ghp_`, and a hit raises `BackupError`, so one mention in any row stops every
later backup.

Change:

1. Replace the patterns with full token shapes:

   | Label | Pattern |
   |---|---|
   | Anthropic API key | `sk-ant-[A-Za-z0-9_-]{32,}` |
   | GitHub token | `gh[pousr]_[A-Za-z0-9]{36,}` |
   | GitHub token | `github_pat_[A-Za-z0-9_]{60,}` |
   | Slack token | `xox[bap]-[0-9A-Za-z-]{20,}` |
   | AWS access key | `\bAKIA[0-9A-Z]{16}\b` |
   | Telegram bot token | `\b\d{8,10}:[A-Za-z0-9_-]{35}\b` |
   | private key | unchanged |

2. A hit no longer stops the snapshot. `export_sql` gets a second pass that
   replaces each match **in the dump text only** with
   `[redacted:<label>]` before the file is moved into place. The live database
   is not touched.
3. `snapshot()` then alerts Admin once per day (existing `_alert_admin`, key
   `dump-redacted`) with the table name and row id of each hit, never the
   value, and continues to the commit.
4. `scan_for_secrets` (store values) keeps blocking the **push** as now.

Done when: tests show (a) a row that merely says "keys start with sk-ant-" is
not a hit, (b) a row with a full-shape fake token is written to the dump as
`[redacted:…]`, the snapshot returns a commit id, and one alert is queued.

## Task B3 — Do not treat waiting items as stalled (L7)

File: `engine/atobots/dispatcher.py`. Tests: `tests/test_dispatcher.py`.

In `_stall_doing`, add to the SQL filter:
`AND (waiting_on IS NULL OR waiting_on='') AND COALESCE(owner_waiting,0)=0`.

Done when: a test with a `doing` item whose `waiting_on` is set, older than
the lease, stays `doing` and records no `item.stall_revert`.

## Task B4 — Wake-cap bypass and alert (L6, L14)

File: `engine/atobots/dispatcher.py`. Tests: `tests/test_dispatcher.py`.

1. `_owner_bypasses_cap`: remove the `from_bot == "owner"` test. The rule is
   now only "Admin's urgent messages bypass the cap". Update the module
   docstring, the `Config` comment and `CHANGELOG.md` wording to match.
2. `_wake_capped`: instead of a message to Admin (a paid wake), record
   activity kind `wake.capped` once per bot per hour (use the existing
   `meta` key pattern `wakecap.<bot>` holding `now[:13]`). `cb doctor` gets a
   finding `wakecap:<bot>` when a bot was capped in three hours of the last
   24 (add the key shape to `doctor.valid_finding_key`).

Done when: tests show no message row is created by a capped bot, the activity
row appears once per hour, and the doctor finding appears at the third hour.

## Task B5 — Keep the top of Soul and rules when over the cap (L5)

File: `engine/atobots/bots.py`, `engine/atobots/config.py`. Tests:
`tests/test_bots.py`, `tests/test_config.py`.

1. `_read_capped(path, cap, keep="tail")`; `identity_prompt` passes
   `keep="head"` for `SOUL.md` and `rules.md`. Persona and Memory keep
   `tail`. When cutting at the head side, cut at the last newline before the
   cap and append the line `[cut: file is over its size limit]`.
2. Add `rules_cap_bytes: int = 4 * 1024` to `Config` and use it.
3. Remove the `print(... file=sys.stderr)` warning; `bots.check()` (used by
   `cb doctor`) already reports over-cap files — add `rules.md` to it.

## Task B6 — Indexes for the tick queries (L10)

File: `engine/atobots/db.py`. Tests: `tests/test_db.py`.

Add to `SCHEMA` and to `_migrate` (all `CREATE INDEX IF NOT EXISTS`):

```
messages_status_to   ON messages(status, to_bot)
messages_chain       ON messages(chain_id)
items_status         ON items(status)
runs_bot_open        ON runs(bot, ended_at)
runs_session         ON runs(session_id)
```

Done when: a test opens a database created without them, calls `connect()`,
and finds all five in `sqlite_master`.

## Task B7 — Small defects (L16)

One commit, each with a one-line test where it has behaviour:

- `routines.py` help for `--check`: "relative path under checks/ in the
  company home (no shell metacharacters or sudo): exit 0 means work to do,
  exit 1 means nothing".
- `routines._cmd_add`: load `Config` once.
- `items.pause`: drop the unused `cfg` parameter (update the caller).
- `cb send --effort`: `choices=runner.EFFORT_ORDER`.
- Remove `runner.LEVEL_ORDER`; use `messages.LEVELS`.
- Remove the unused `lane` parameter of `runner.build_command` (update
  `runner.run` and tests).
- `items.archived_at`: have `archive.archive_old_records` set it for the item
  rows it writes to the archive, in the same transaction as the delete (the
  delete then matches on it). If that module's flow makes this larger than 20
  lines, stop and leave a note in the PR instead.

## Out of scope

- Authorisation changes (Spec A).
- `update.sh`, schema version, templates refresh (Spec C).
