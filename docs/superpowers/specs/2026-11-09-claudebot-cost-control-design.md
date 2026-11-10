# ClaudeBot engine: cost control and thinker/worker tiers

Date: 2026-11-09. Scope: the engine in `engine/` only. Which company bots exist,
what they do, and their personas are out of scope.

## Why

The Owner runs ClaudeBot on a Claude subscription and is burning the usage
limit far too fast. Findings so far (read-only, from the VPS and the code):

- The only transcripts with real usage are the two Admin sessions. One ran
  72 turns and about 3.8M cache-read tokens (about 52k per turn); the other,
  on Opus 5.5, 29 turns and about 1.3M. The bots `bi`, `ceo`, `rb` have never
  run. Admin turns are the whole bill so far.
- Even a one-word reply from the Owner ("Yes", a thumbs up) is a full turn: it
  re-reads the whole session context, and rewrites it if the cache expired.
- A separate Mac session showed the same mechanism at its worst: one "Try
  again" on a 723k-token context after the cache expired rewrote the whole
  context. Cache reads cost 0.05x the normal input price on Opus 5.5 and
  Sonnet 5.5; a 5-minute write costs 1.25x and a 1-hour write 2x. The cost is
  in cache writes (new sessions, expired caches), not in cache reads.
- The engine has no worker tier. Every item runs as the bot that owns it, on
  that bot's model. `_wake_assigned` wakes an owner for any todo item;
  `_wake_admin` wakes Admin when open work changes; each bot may be woken up
  to 12 times an hour (`wakes_per_bot_per_hour`). A run's level can raise the
  model to Opus (`LEVEL_MODEL` in `runner.py`).
- Rotation exists (`chat_rollover_tokens` = 100k) but it relies on Claude
  writing its own summary, which is itself a costly turn, and it does not look
  at idle time.

Anthropic does not publish how the subscription meter weighs models. At API
prices a cached read on Haiku 4.5 is only about half the price of Opus 5.5, so
the saving from cheap workers must be measured, not assumed. One unverified
GitHub report says a subagent's `model:` setting was ignored inside a session
and billed as the parent model.

## Goals

1. Thinkers decide, workers do. Thinkers write precise tasks and are woken only
   for judgment. Workers run those tasks on a cheap model, as separate
   processes.
2. Over 90% of input tokens are cache reads, by construction.
3. Fewer, bigger requests: one thorough task prompt, batched messages, no
   chatter turns.
4. The Owner never pays a turn for an unneeded "yes" or acknowledgement.
5. Know where the usage goes: a ledger of tokens, cache, model and trigger for
   every run and every Admin turn, and a `cb usage` report.

## Non-goals

- Shrinking Admin's startup context (the Owner excluded it).
- A usage budget that pauses bots (the Owner excluded it).
- Choosing which company bots exist or what they do.
- Changing the publishing, spending and Owner-word rules in `templates/rules.md`.
- Using the Anthropic API instead of the subscription login.

## Anti-goals

- A cost saving that weakens a rule about publishing, spending or the Owner's
  word.
- A worker or thinker that silently runs on a different model than the one set.
- Hidden loops: a cheaper run that wakes a thinker again and again.

## The design

### 1. Tiers

- **Thinker** bots: model floor Sonnet, never Haiku. Today the Owner has set
  Admin and CEO to Opus 5.5 and BI and TL to Sonnet; the engine reads these
  from each bot's `model` field and does not hard-code names.
- **Worker** bots: model Haiku 5.5. The researcher is one. A worker is a
  fixed, named bot like any other (role, soul, skills, model); the engine never
  creates agents on the fly.
- Each bot gets a `tier` (`thinker` or `worker`) in the bots table. The engine
  enforces: thinker model is sonnet or opus; worker model is haiku.
- A run's level may lower the model of a thinker run to Haiku only for
  `housekeeping`. A level never raises a run above the bot's configured model.
  The `deep` and `heavy` levels keep their other effects (lane, effort) but no
  longer pick Opus. (This changes `LEVEL_MODEL` and `settings_for`.)
- At every start the engine compares each bot's model with its tier rule and
  records any mismatch in the activity log (`doctor` also shows it). It does
  not silently change a model.

### 2. Task packets and hand-off

- A thinker turns an item into a task packet for a worker: goal, input file
  paths, output file paths, and a "done" check, in one message. The message has
  a new `kind` (`task`); the dispatcher routes a `task` message to a worker.
- The worker runs it in a fresh short session and answers with a `result`
  message (done, failed with reason, files written). Only then is the thinker
  woken.
- Items that are only waiting (todo or next with an assigned owner) wake a
  worker, not the thinker owner. `_wake_assigned` changes to this. The thinker
  is woken on: a worker result or failure, a new Owner request, a decision that
  is the Owner's, or a routine due for a thinker.
- Wake caps are per tier: thinker bots get a much lower hourly cap than
  workers (defaults: Opus bot 3, Sonnet bot 6, Haiku bot 12; configurable in
  `config.json`). `_wake_admin` keeps its once-an-hour limit and also needs a
  real change in open work.

### 3. Cache by construction

- The identity prompt (persona, soul, rules, skills, tools) for a bot is built
  deterministically and is byte-identical across runs; anything that changes
  between runs (task, time, counters) goes only in the task packet, after the
  stable prefix. A test builds the prompt twice and compares bytes.
- Runs for the same bot are batched. `batch_delay_seconds` rises from 5 to 60
  for worker bots so several messages land in one run, and runs of one bot are
  started back to back so the 5-minute cache is still warm.
- Admin's always-on session asks for the 1-hour cache (an environment setting
  on `claude`; the builder confirms the exact switch). Worker runs use the
  default 5-minute cache.
- The ledger (section 5) reports the cache hit rate; the target is 90% or more
  of input tokens read from cache on Admin and on warm batches.

### 4. Session rotation by the engine

- For the always-on Admin and `cb chat`, the engine watches the transcript. It
  rotates (restarts with a short hand-off summary) when the context passes
  `chat_rollover_tokens`, and also when the session has been idle longer than
  the cache lifetime and is large. The next message then starts a fresh,
  small session and does not rewrite a huge context. The summary is written by
  the engine from the transcript and the tracked items, not by asking Claude in
  a turn. (This replaces the nag in `chat.py`'s Stop hook; the rollover file
  format stays.)
- Idle rotation never interrupts a turn in progress or loses a pending Owner
  message.

### 5. Usage ledger and `cb usage`

- A `usage` table: time, bot, model, lane, trigger (owner message, wake kind,
  routine, worker result), run or session id, input, cache write, cache read,
  output tokens. Worker runs are recorded from the run's JSON result; Admin and
  chat turns are read from the session transcript.
- `cb usage [--since 24h] [--by bot|model|trigger]` prints totals and the cache
  hit rate. It flags any run whose model differs from the bot's configured
  model, which also tells the Owner whether a model setting is really honored.

### 6. Fewer questions to the Owner

- `templates/` rules and the live-Admin prompt (`LIVE_PROMPT` in
  `admin_live.py`) change to: act without asking on reversible, low-risk work;
  ask only for Owner-only actions (publish, spend, sign up, commit to others)
  and for real choices; put all open questions in one message; never reply to
  a bare "yes", "ok", thumbs up or other acknowledgement unless it answers an
  open question; never ask "should I proceed?" for work already requested.
- The engine drops the pending-question text that only restates a request.
  (Wording is left to the builder; the rule above is the requirement.)

## Constraints

- Runs on the Debian 13 VPS, 2 vCPU, 2 GB RAM; Python, SQLite, `claude` CLI on
  the subscription login. No new services.
- `engine/` has about 270 tests, 15 skipped on the Mac for GnuPG; they must keep
  passing, with new tests for each section.
- Existing data must migrate: the new `tier` and `usage` schema changes are
  additive, with a default tier inferred from a bot's current model.

## Decided by the Owner

- Thinkers at least Sonnet, workers Haiku 5.5; Admin and CEO on Opus 5.5.
- Fixed SME agents, not dynamic ones.
- Aim for over 90% cached input, fewer and bigger requests.
- No unneeded confirmation turns.
- Not in scope: smaller Admin startup context, a usage-budget pause.

## Left to the builder

- Exact table and column names, the migration, and `cb usage` output format.
- How to read the 1-hour cache setting for the `claude` CLI.
- How the engine summarizes a transcript for rotation.
- Default wake caps (the numbers above are my guess, not the Owner's).
- Test layout.

## My calls (not yet confirmed by the Owner)

- Tier is a new field, not inferred from the model each time. For existing
  bots the migration sets it from the model (haiku = worker, else thinker);
  a mismatch with the rules is logged, never changed.
- Level no longer raises the model; `deep` and `heavy` keep lane and effort.
- Wake caps by tier and the 60-second worker batch delay.
- **Stable prefix.** Only persona, soul, rules, skills and tool settings are in
  the stable prefix (they change rarely, and a change is an accepted one-time
  cache miss). `MEMORY.md`, the list of other bots (`agents_json`) and the
  task move out of the prefix: the task packet starts with them or with a
  pointer to read `MEMORY.md`. The byte-identical test covers the prefix with
  those files changed.
- **Workers.** The engine does not create worker bots; it enforces rules on the
  bots that exist. A thinker may have a default worker (new optional bot field
  `worker`). An item assigned to a thinker with a worker wakes the worker, using
  a packet the thinker wrote when it assigned the item, or the item text if
  there is none. With no worker, the owner is woken as today and `cb usage` and
  `cb doctor` flag "no worker set".
- **Done check.** The worker reports; the engine also checks that the output
  files named in the packet exist, and counts the run as failed if not.
- **No hidden loops.** `task` and `result` messages count against the existing
  `hop_limit` (they are not `_NEVER_STOP`). After 2 failed attempts on one item
  the engine stops retrying and sends one message to Admin (the Owner if the
  owner is Admin).
- **Caps.** A message from the Owner to a bot bypasses its wake cap; system and
  bot-to-bot wakes obey it. A capped thinker's work stays queued as today.
- **Model check.** The model that served a run comes from the run's JSON result
  (`modelUsage`) or the transcript's `message.model`; the builder confirms which
  on the VPS. A mismatch is reported in `cb usage` and `cb doctor`, and does not
  fail the run. In-session subagents are out of scope.
- **Rotation.** The size test is context above 80k tokens (the Admin baseline is
  about 50k, so a fresh session is cheaper above that); idle means longer than
  the cache lifetime (1 hour if the 1-hour cache can be set for the live Admin,
  else 5 minutes). Both numbers are `config.json` settings.
- Engine-written rotation summary instead of Claude-written (open items, the
  Owner's last messages, pending questions, recent decisions).
