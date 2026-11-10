# Changelog

Each release has a section here. "Manual steps after updating" lists anything
`engine/update.sh` does not do for you. How to update: [UPDATING.md](UPDATING.md).

## 0.0.2 - 2026-11-10

The review-fixes release: tighter working rules around who may do what, fewer
ways for work to be lost or looped, and an install that is no longer tied to
one server account. No new bot features.

### Security
- `--from` is locked to the calling bot; decisions, Soul changes and
  owner-order-only secrets need an Owner session.
- The task packet is passed in a private file, not on the command line.
- Archive months are scanned for store secrets before they are pushed, and
  backup pushes are never forced.
- Routine check commands are limited to files under the company home and are
  recorded in full.
- The Telegram token file is removed on every exit path; the always-on Admin
  does not start without a working token.
- MCP definitions name a store item as `${secret:NAME}`; values are filled in
  per run and each use is recorded.
- A Telegram message from a sender who is not on the channel's allow list is
  refused before it reaches Admin and is never stored as an Owner request.
- Only Admin or a person at the server may close another bot's message or
  retry a failed one (`cb message done`, `cb message retry`).

All bots still run under one server account that has sudo, so these are
working rules that the record backs up, not technical locks.

### Reliability
- Loop guard covers plain messages between two bots; unanswered questions and
  stalled items are reported; split items wake their owner.
- Only one Admin runs at a time; with the always-on Admin up, queued messages
  are surfaced to it and to Telegram.
- A down dispatcher is reported outside the message queue (file and Telegram).
- Chat and always-on Admin sessions have a memory limit.
- `cb backup restore --archive SOURCE` also brings back old transcripts and
  records.
- An item left in `doing` with no run for longer than the lease goes back to
  `todo` and its owner is woken again; Admin is told when it keeps happening.
- A question or task is closed only when the bot replied, finished the task or
  updated the item; otherwise it goes back to the queue.
- Archive pushes can be repeated safely, and the current month is mirrored to
  the `archive` remote every hour.
- `cb bot relink` recreates every bot's skill links.

### Reuse
- The server account, its home, the `claude` path, the run memory limit, the
  default model and the timezone can be set in `config.json` (`run_user`,
  `run_home`, `claude_bin`, `run_memory_max`, `default_model`, `timezone`).
  The account defaults to whoever runs `cb`.
- Service units are written for the account that runs `install.sh`.

### Updating
- `engine/update.sh` now waits for bot runs to finish (up to 30 minutes),
  installs changed unit files, and refreshes the engine-owned `rules.md` and
  bundled skills in the company home (`cb templates refresh`).
- `cb dispatcher idle` exits 0 when no bot run is active.
- The database gains one column (`decisions.owner_session`); it is added
  automatically the first time the new engine opens the database.

### Manual steps after updating
From 0.0.1, the updater that runs is the 0.0.1 one, which does not do the new
steps above. After `engine/update.sh` finishes, run these once
([UPDATING.md](UPDATING.md) has the details):

1. `engine/install.sh --start "<company name>" "$CLAUDEBOT_HOME"` — installs
   the new unit files.
2. `cb templates refresh` — replaces `rules.md` and the bundled skills in the
   company home with this release's. If you edited `rules.md`, save your copy
   first.
3. `cb bot relink`
4. Compare Admin's persona with the new starting file and apply it only if
   the Owner agrees:
   `diff "$CLAUDEBOT_HOME/bots/admin/PERSONA.md" engine/templates/admin/PERSONA.md`

### Known issues
- A question or task that a bot never answers is retried without limit (up to
  the hourly wake cap) instead of failing after three attempts.
- The hourly backup refuses to commit if any stored text contains the start of
  a known token shape (for example the letters a GitHub token begins with),
  even when it is only a mention.
- `engine/update.sh` does not reinstall the always-on Admin's unit file; run
  `engine/install.sh` again when a release says the units changed.
- `cb hook stop` was removed. Nothing in this release calls it.

## 0.0.1 - 2026-11-10

The cost-control release.

### Cost control
### Cost control
- Bots have a tier: thinkers decide, workers do routine tasks on a cheaper
  model. A run's level never raises a bot's model.
- The cached part of every prompt stays byte-identical between runs; Memory
  and the list of colleagues travel in the task packet.
- `cb usage` reports tokens, cache hits and the model that really served
  each run.
- Hourly wake caps depend on the model (Opus 3, Sonnet 6, Haiku 12); Owner
  messages and Admin's urgent wakes bypass them. Worker messages wait 60 s so
  several land in one run.
- The engine restarts a long session itself, with a summary built from the
  records: when the context passes `chat_rollover_tokens`, or when it is over
  `idle_rotate_tokens` and idle longer than `cache_ttl_seconds`. The Stop hook
  no longer asks Admin to write a summary.
- A bare "ok", "thanks" or thumbs up from the Owner is not sent to the model
  unless a question is open (`cb question open`) or Admin's last message
  asked one.
