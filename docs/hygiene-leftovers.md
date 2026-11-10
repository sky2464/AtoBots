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

## Strip HTML comments at Persona prompt assembly

- Admin/Backend `PERSONA.md` templates start with HTML comments meant for humans.
- Stripping them belongs in `bots.identity_prompt` (and any shared reader).
- **Skipped in PR6:** `bots.py` is owned by the authz / bot-identity PRs.
- Follow-up: strip `<!-- ... -->` (including multiline) from Persona text only,
  after the cap read, before the identity prompt is assembled.

## Dedupe `_actor` / `_run` and bot-name regexes

- Several modules define nearly identical `_actor()` / `_run()` helpers
  (`bots`, `items`, `messages`, `routines`, `store`, …).
- Bot-name regexes differ on purpose today (`bots._NAME_RE` vs `store._NAME_RE`).
- Deduping touches files owned by PR1–3; leave until those land, then extract a
  shared CLI helper if still duplicated.

## Unused `lane` label

- `runner.build_command` / `runs.lane` still label chat/live/heavy/normal runs.
- Doctor and dispatcher query those labels; **not dead** — left alone.

## `_remotes` alias

- Removed in PR6 (`backup._remotes = remotes` had no callers).
