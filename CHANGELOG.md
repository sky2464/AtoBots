# Changelog

## 0.0.1 (not yet tagged)

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

### Reliability
- Loop guard covers plain messages between two bots; unanswered questions and
  stalled items are reported; split items wake their owner.
- Only one Admin runs at a time; with the always-on Admin up, queued messages
  are surfaced to it and to Telegram.
- A down dispatcher is reported outside the message queue (file and Telegram).
- Chat and always-on Admin sessions have a memory limit.
- `cb backup restore --archive SOURCE` also brings back old transcripts and
  records.

### Reuse
- The server account, its home, the `claude` path, the run memory limit, the
  default model and the timezone can be set in `config.json` (`run_user`,
  `run_home`, `claude_bin`, `run_memory_max`, `default_model`, `timezone`).
  The account defaults to whoever runs `cb`.
- Service units are written for the account that runs `install.sh`.

### Upgrade notes
- Run `engine/install.sh` again after deploying: the unit files changed.
- Sessions started by an older engine still call `cb hook stop`; it now does
  nothing.
