# Hygiene leftovers (deferred from PR6)

PR6 ships CI, docs, version, and only dead-code edits that do not fight the
security/logic PRs. These items stay intentional follow-ups:

## `items.archived_at` has no writer

- Schema and many readers filter `archived_at IS NULL`
  (`items`, `dispatcher`, `doctor`, `archive`).
- Nothing in the engine sets `archived_at` today (tests plant it by hand).
- **Do not drop the column** while readers rely on it.
- Prefer a small write in the archive path when that module is next touched
  (likely the backup/archive PR), not a drive-by schema change here.

## `_run` helpers still differ per module

- `_actor()` is now one helper (`activity.actor`); `messages` keeps its own,
  stricter one on purpose.
- The `_run()` wrappers catch different exception sets per module and were
  left alone.

## Unused `lane` label

- `runner.build_command` / `runs.lane` still label chat/live/heavy/normal runs.
- Doctor and dispatcher query those labels; **not dead** — left alone.

## `_remotes` alias

- Removed in PR6 (`backup._remotes = remotes` had no callers).
