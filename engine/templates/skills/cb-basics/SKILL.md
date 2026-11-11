---
name: cb-basics
description: How to use the `cb` command to find work, message other bots, search, file notes, keep secrets and check the company. Use whenever you need to run `cb`.
---

# cb basics

`cb` is the company's one command. It reads and writes the company database
and the activity log. Run `cb <command> --help` for the details of any
command. Everything you do through `cb` is recorded under your name.

## Find your way

- `cb status` shows the state of the company.
- `cb next` lists what to work on, Owner-waiting first. `cb open` lists every
  open item. `cb item show T-0007` shows one item.
- `cb inbox` shows your unfinished messages. `cb inbox --full` shows them whole.
- `cb search "words"` searches items, messages, decisions, notes and old
  conversations. Narrow it with `--kind` (knowledge, item, message, memory,
  transcript, decision) or `--bot NAME`.
- `cb bot list` and `cb bot show NAME` show the bots and how they are set up.
- `cb bot check` checks every bot's files (size limits, Soul changes).
- `cb bot relink` recreates skill symlinks after an engine move or update.

## Talk to other bots

- `cb send NAME "subject" "body" --item T-0007` leaves a message and wakes the
  bot. Point to files (`work/T-0007/script.md`) instead of pasting them.
  Bodies stay short. Never send thanks or acknowledgements.
- A message is sent only when the command prints an id. Any other result
  means it was not sent. Do not tell the Owner it was passed on until you have
  the id.
- Do not reply to a reply or a note unless it needs something done. Ending
  the run without replying is correct.
- `cb ask NAME T-0007 "question" --handoff-file PATH` asks and then ends your
  session. The handoff file says where you stopped and what to do on the
  answer. You are woken with the answer and your note.
- `cb reply MESSAGE_ID "answer"` answers a message. Use `--file PATH` for a
  longer answer.

## Setup files

- An MCP server definition is `$CLAUDEBOT_HOME/mcp/NAME.json`; a plugin is the
  folder `$CLAUDEBOT_HOME/plugins/NAME/`. Name them with `cb bot new` or
  `cb bot set` (`--mcp`, `--plugins`).
- A token an MCP server needs is written in the definition as
  `${secret:NAME}` (the name from `cb secret list`). The value is filled in
  when the run starts and the use is recorded. Never write a token value
  into an MCP file.
- `--connectors` on a bot exposes every account connector the Owner has linked,
  not just one. Ask the Owner before turning it on.

## Remember

- `cb note add "title" --body-file PATH` files a note under your own
  knowledge folder. Use it for what others, or you in a later session, will
  need to find.
- Memory is the file `MEMORY.md` in your working folder. Keep it short and
  current. Never put a secret in it.
- A decision is bound to this session. Record it with `cb decision record`.

## Secrets

- `cb secret list` shows names only. `cb secret add NAME --kind token` asks for
  the value privately. The Owner types it themselves in their terminal. Never ask
  them to say a secret in chat, and never write one down.
- `cb secret run --env VAR=NAME -- command` runs one command with the secret in
  its environment, so it never appears on screen. A secret marked Owner's order
  only also needs `--decision D-0001`, the recorded decision.

## Routines

- `cb routine add BOT "0 7 * * *" "what to do"` adds a schedule (the company's time zone).
  `cb routine list`, `cb routine off ID`, `cb routine on ID`, `cb routine rm ID`.
  Add `--check "command"` so it wakes the bot only when there is work.

## System changes

- Before any system change (stop, restart, move, delete, install), write the plan
  with `cb change plan "TITLE" --steps-file FILE --touches PATH_OR_UNIT...` and get
  the Owner's approval with `cb change approve C-N --quote "<exact words>"`.
- `cb change show C-N` shows a planned change.
- `cb verify services` verifies dispatcher and admin services are active.
- `cb change done C-N` closes an approved change after verifying services (`--failed "reason"` to close as failed).
- `cb update TAG` updates ClaudeBot in a standalone unit.

## Configuration and Rollover

- `cb config get chat_rollover_tokens` shows the token limit where session rollover happens.
- `cb config get idle_rotate_tokens` shows the token size above which an idle session rotates.
- `cb config get cache_ttl_seconds` shows the prompt cache lifetime in seconds.
- `cb config get rollover_handoff_seconds` shows how long the engine waits for your handoff summary.

## Times

Say times to people like `Tue Nov 3, 2026, 7:45 AM` in the company's time zone.
