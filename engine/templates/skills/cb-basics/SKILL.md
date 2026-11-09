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
- `cb check` checks every bot's files (size limits, Soul changes).

## Talk to other bots

- `cb send NAME "subject" "body" --item T-0007` leaves a message and wakes the
  bot. Point to files (`work/T-0007/script.md`) instead of pasting them.
  Bodies stay short. Do not send acknowledgement-only messages.
- `cb ask NAME T-0007 "question" --handoff-file PATH` asks and then ends your
  session. The handoff file says where you stopped and what to do on the
  answer. You are woken with the answer and your note.
- `cb reply MESSAGE_ID "answer"` answers a message. Use `--file PATH` for a
  longer answer.

## Remember

- `cb note add "title" --body-file PATH` files a note under your own
  knowledge folder. Use it for what others, or you in a later session, will
  need to find.
- Memory is your own file. Keep it short and current. Never put a secret in it.

## Secrets

- `cb secret list` shows names only. `cb secret add NAME --kind token` asks for
  the value privately. The Owner types it himself in his terminal. Never ask
  him to say a secret in chat, and never write one down.
- `cb secret run --env VAR=NAME -- command` runs one command with the secret in
  its environment, so it never appears on screen. A secret marked Owner's order
  only also needs `--decision D-0001`, the recorded decision.

## Routines

- `cb routine add BOT "0 7 * * *" "what to do"` adds a schedule (Central Time).
  `cb routine list`, `cb routine off ID`, `cb routine on ID`, `cb routine rm ID`.
  Add `--check "command"` so it wakes the bot only when there is work.

## Times

Say times to people like `Tue Nov 3, 2026, 7:45 AM CT`.
