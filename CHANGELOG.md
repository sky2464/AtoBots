# Changelog

## 0.0.1 - 2026-11-10

The cost-control release. It changes how bots are tiered and woken, how they
reuse their prompts, how usage is recorded, and how long sessions are kept.

### What changed

- **Thinker and worker tiers.** Every bot is a thinker (plans and decides on the
  big model) or a worker (does routine work on the small model). A thinker can
  hand routine work to a named worker through a new `worker` field.
- **Models follow the bot.** A bot's model is set by its tier and the Owner. A
  bot's level no longer raises its model.
- **Stable cached prompt prefix.** The fixed part of every prompt is the same
  from run to run, so the server can reuse it. Memory and colleague lists moved
  out of that fixed part into the per-task packet.
- **Usage ledger and `cb usage`.** Every run records its tokens and cache use.
  `cb usage` shows the totals, the cache hit rate, and a report of runs whose
  model did not match the bot's setting.
- **Worker task and result messages.** A thinker's handoff to a worker is a task
  message, and the worker's answer comes back as a result message. If a task
  fails, it is escalated to the thinker instead of being dropped.
- **Wake caps per model.** Each hour, a bot may be woken only so many times,
  with a lower cap for bigger models (opus 3, sonnet 6, haiku 12 by default;
  `config.json` can change them). The Owner's messages, and urgent messages from
  Admin, bypass the caps. Workers wait longer (60 seconds by default) before a
  run, so several task messages go in one run.
- **Engine-driven session rotation.** The engine starts a fresh session for a
  long-running Admin when the session reaches 100,000 tokens (by default), or when
  it has at least 80,000 tokens and has been idle longer than the cache lifetime.
  The live Admin session keeps its prompt cache for one hour
  (`CLAUDE_CODE_PROMPT_CACHE_TTL=1h`).
- **Stop hook removed.** Rotation is done by the engine, so the old Stop hook
  that could block a reply is gone.
- **`cb question` and the bare-acknowledgement filter.** Questions to the Owner
  are registered with `cb question open`. A bare "yes", "ok" or thumbs-up is refused
  when no question is open, so it cannot start work by accident.
- **"Ask the Owner less" rules.** The company rules (`rules.md`) now say:
  act without asking on reversible, low-risk work; ask only about publishing,
  spending, signing up, commitments to other people, and real choices; put every
  open question in one message with a recommended answer first; never ask
  "should I proceed?" for work already requested.

### Manual steps after updating

1. **Copy the updated rules file.** Copy `engine/templates/rules.md` over your
   company home's `rules.md`. Check first that you have not edited your copy; if
   you have, merge the changes by hand. The update does not rewrite `rules.md`.

   ```bash
   cp /home/claude/MyCompany/rules.md /home/claude/MyCompany/rules.md.bak
   ```

   ```bash
   cp ~/ClaudeBot/engine/templates/rules.md /home/claude/MyCompany/rules.md
   ```

   Use your own paths (your clone may be somewhere else). Keep the `.bak` copy
   until you have read the new file.

2. **Update Admin's "Asking him things" wording.** Admin's persona text lives in
   the database, so the update does not change it. The new wording is in
   `engine/templates/admin/PERSONA.md` under "Asking him things". Apply it only
   with the Owner's agreement.

### Known limit

- The acknowledgement filter refuses a bare "yes" when Admin asked a question
  without registering it with `cb question open`. Until Admin registers its
  questions, the Owner's one-word answer may be refused. Ask Admin to register the
  question again, or reply in a full sentence.
