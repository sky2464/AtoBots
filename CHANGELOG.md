# Changelog

Each release has a section here. "Manual steps after updating" lists anything
`engine/update.sh` does not do for you. How to update: [UPDATING.md](UPDATING.md).

## Unreleased

Updating an old install works again, in one command.

### What changed
- `engine/adopt.sh --repo URL` now takes an install older than 0.1.0 to the
  newest release by itself: it adopts into v0.1.0 first, then updates to the
  release asked for. Each step keeps its own backup and rollback. This replaces
  the 0.2.1 advice to reinstall fresh and re-enter every secret.
- `engine/adopt.sh --detach` runs the job as its own systemd unit, so it
  survives Admin being stopped.
- `engine/MIN_UPDATE_FROM` names the oldest version a release updates from
  directly. `cb update --check` says "go through v0.1.0 first" for an install
  below it, and an update that is started anyway stops before changing
  anything and rolls back with the same message.

### Manual steps after updating
- None.

## 0.2.1 - 2026-11-11

Housekeeping: the compatibility code for installs older than 0.1.0 is gone,
and the README is ready for a first-time reader.

### What changed
- Only `ATOBOTS_HOME` and `/etc/atobots.env` are read, and bot runs get only
  `ATOBOTS_HOME`. The `0.1.0` migration, the leftover-service check in
  `cb doctor` and the handover from very old updaters were removed.
- Installs older than 0.1.0 can no longer be updated in place. Install the
  current release fresh and restore the company home from a backup.

### Manual steps after updating
- None, if the install is already on 0.1.0 and `cb doctor` was quiet.

## 0.1.0 - 2026-11-10

The project is now **AtoBots**. Nothing about what the engine does changed in
this release.

### What changed
- The services are `atobots-dispatcher`, `atobots-admin`, `atobots-backup`
  and `atobots-doctor`.
- The company home variable is `ATOBOTS_HOME` and the env file is
  `/etc/atobots.env`.
- The Python package folder is `engine/atobots/`.
- The release-key setting is `atobots.releaseKey`.

### Fixed
- A zip with links inside is refused outright. Before, a link that stayed
  inside the build was allowed, and two such links could lead out together.
- Installing a local build no longer loses files that the server's own Git
  ignore lists match, and no longer commits Python cache files.
- Admin's folder-trust question is recognised by its own wording only; the
  words "do you trust" in an ordinary conversation no longer count.

### Also new
- [SECURITY.md](SECURITY.md): what the engine does and does not protect, in
  plain words, and how to report a problem.
- [docs/public-readiness.md](docs/public-readiness.md): what still stands
  between this repository and a public one.

## 0.0.3 - 2026-11-10

The smooth-updates release. Updating is now one job that looks first, tells
the Owner, and puts everything back by itself if it fails. Admin also shows a
live status line in Telegram while it works.

### Updating
- `cb update --check` says what an update would do and changes nothing: what
  is running and where it came from, the target and whether it is newer, how
  many files differ, whether templates, unit files, the database or a
  migration step are involved, what restarts and for how long.
- `cb update` (the newest release) or `cb update TAG` runs the update in its
  own job. The Owner gets a Telegram message before the chat goes offline.
- **Automatic rollback.** If a step fails after the code was switched, or the
  services do not come back and stay up, the job restores the old code, the
  database copy taken before the update and the old unit files, starts the
  services and says so in Telegram. The rollback is tried once; if it fails
  too, the job stops and reports that, with the manual steps in the log.
- `cb update --adopt --repo URL` turns a copied install (no `.git`) into a
  Git clone in one job, keeps the old folder as `<folder>.old`, and goes back
  to it if the adoption fails. `engine/adopt.sh` does the same from a
  terminal for installs too old to have the command.
- A zip or folder can be installed as a local build (`cb update --zip FILE`).
  It is checked before unpacking, marked as unverified, reports its version
  as `<version>+local.<hash>`, and needs an approved change plan when a bot
  starts it. This is for maintainers.
- A release can ship migration scripts in `engine/migrations/`; the update
  runs the ones between the old and the new version.
- Each version finishes its own update: the installed version does the first
  half, then starts `engine/lib/update_finish.sh` from the new one.
- The backup and health-check timers are stopped during the switch, and every
  step that could hang has a time limit.

### Fixed
- The installer could overwrite the engine's own `cb` script: where
  `/usr/local/bin/cb` was an old symlink into the clone, writing the wrapper
  went through the link. Files under `/usr/local/bin`, `/etc` and the systemd
  folder are now replaced, never written through.
- After a restart Admin could sit on Claude Code's folder-trust question with
  Telegram silent. The update's finish message now says so and gives the fix,
  and the engine no longer types its `[system]` notices into that question
  (the line and its Enter would have answered it).
- An error handler in the updater could run inside a sub-command and release
  the queue hold early.
- The update's scratch files could be swept into the company's Git history.
- `cb doctor` findings that were there before an update are reported apart
  from the update's own result.

### Telegram status
- While the always-on Admin works on a Telegram message from the Owner, the
  chat shows "typing", then one silent message such as
  "Reading files · 2 min · 1 more waiting". It is edited as the work moves on
  and removed when the turn ends, so the reply arrives as a normal message.
- The labels are fixed words chosen from the kind of step. No file name,
  command or message text is ever shown, and no model tokens are used.
- Set `"status_peek": "off"` in `config.json` to turn it off.

### Also in this release (built since 0.0.2)
- System changes need a written plan and the Owner's recorded yes
  (`cb change plan`, `cb change approve`, `cb change done`);
  `cb verify services` checks the services afterwards.
- The health check alerts and never restarts a service.
- Sent messages are confirmed, requests are linked to items automatically,
  and `cb digest` gives a daily "what changed".
- `cb transcribe` turns a voice message into text; `cb backup files` makes an
  encrypted backup of files.
- A long session writes a handoff before it is restarted.
- `python` is made to resolve to `python3`, and `cb doctor` reports hook
  commands that cannot run.

### Manual steps after updating
- **Coming from 0.0.2 or 0.0.1:** update to this release with the old
  updater and name the tag: `engine/update.sh --tag v0.0.3`. Then follow
  "Updating from 0.0.2 or earlier" in [UPDATING.md](UPDATING.md).
- Admin's starting persona and the rules gained one line about
  `cb update --check`. An edited `rules.md` is left alone (`rules.md.new` is
  written beside it); a persona is changed only with the Owner's agreement.

### Known issues
- The status line covers the always-on Admin only. Work a bot does after
  Admin handed it over is not shown.
- The status line depends on the format of Claude Code's session file. If
  that changes, the line falls back to "Working" with the elapsed time.
- Pre-answering the folder-trust question was left out on purpose: it would
  mean editing Claude Code's own settings file. The update reports it instead.

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

1. `engine/install.sh --start "<company name>" "$ATOBOTS_HOME"` — installs
   the new unit files.
2. `cb templates refresh` — replaces `rules.md` and the bundled skills in the
   company home with this release's. If you edited `rules.md`, save your copy
   first.
3. `cb bot relink`
4. Compare Admin's persona with the new starting file and apply it only if
   the Owner agrees:
   `diff "$ATOBOTS_HOME/bots/admin/PERSONA.md" engine/templates/admin/PERSONA.md`

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
- Hourly wake caps depend on the model (Opus 3, Sonnet 6, Haiku 12); Admin's
  urgent wakes bypass them. Worker messages wait 60 s so
  several land in one run.
- The engine restarts a long session itself, with a summary built from the
  records: when the context passes `chat_rollover_tokens`, or when it is over
  `idle_rotate_tokens` and idle longer than `cache_ttl_seconds`. The Stop hook
  no longer asks Admin to write a summary.
- A bare "ok", "thanks" or thumbs up from the Owner is not sent to the model
  unless a question is open (`cb question open`) or Admin's last message
  asked one.
