# Telegram status peek — design

Status: **FUTURE.** Do not build yet. It is small and independent, but it
reaches the live install through the new update flow, so it waits for
[smooth updates](2026-11-10-smooth-updates-design.md) to ship.

## Why

When the Owner asks Admin for something, or several things at once, the chat
can stay silent for minutes. The only sign that anything is happening is the
eyes reaction on the Owner's message. Then the whole reply arrives at once.

The Owner's words: sending small updates costs tokens and usage, but waiting
with no sign of life is worse. What would make the wait feel fine is a peek at
what Admin is doing right now, such as "reading files" or "running tests".

## Goal

While Admin or a bot is working on something the Owner asked for, the Telegram
chat shows a live, plain-language status of what it is doing, and it costs
**no model tokens**.

## Non-goals

- Explaining or summarising the work (that needs the model, so it costs).
- Showing file names, command text, message text or any tool arguments.
- Changing what Admin says in its real replies.
- Updates for work nobody asked for (routines, background digests).

## Anti-goals

- Status that leaks something private or secret into the chat.
- A flood of messages, or notification sounds for every step.
- Any change that slows Admin down or can crash a run when the status code
  fails. A failure in the status path must be silent and must never block the
  work.

## How it can work at zero token cost (from reading the engine)

- The engine already posts to Telegram without the model: `notify_telegram`
  in `admin_live.py` calls the Bot API `sendMessage` with the stored token.
- The always-on Admin is an interactive Claude Code session started with the
  Telegram channel plugin (`chat.py`). Claude Code writes every step it takes
  into its session transcript file, and the engine already knows where those
  live (`claude_projects_dir`, used by transcribe and rotation). A small
  watcher can read new lines of that file and see "a tool was called" events.
  Reading a file is not a model call.
- Bot runs started by the dispatcher use `--output-format json` (one result at
  the end). Switching those runs to the streaming output format would expose
  the same step events as they happen.
- Telegram offers free calls for this: the "typing" action (lasts about five
  seconds, so it is repeated while work continues), editing a message the bot
  sent, and sending a message silently.
- These points are from reading the code; none of it has been tried. The
  transcript format is not a stable public interface, so the parser has to be
  tolerant and fail quietly.

## Design

1. **Trigger.** When an Owner message arrives and work starts, the status
   starts. It ends when the final reply is sent or the work stops.
2. **One status message, edited.** The engine posts a single message, sent
   silently ("Working on it"), and edits it as steps happen. When the real
   reply arrives, the status line is removed or left as one short line, so the
   reply is a normal new message with a normal notification.
3. **Fixed labels, not model text.** A small table maps a step type to a
   label: read a file → "Reading files", search → "Searching", edit → "Editing
   files", run a command → "Running a command", web → "Looking something up",
   a sub-agent → "Asking a helper", otherwise "Working". Specific enough to
   feel alive, never including arguments. Running tests could be recognised
   only from a fixed list of known test commands, and otherwise stays
   "Running a command". (My call.)
4. **Elapsed time and queue.** The line carries the time so far, and says when
   more requests are waiting behind this one ("2 more waiting").
5. **Rate limits.** Edit at most every few seconds, only when the label or
   the minute changes, and not at all for work that finishes within about ten
   seconds.
6. **Typing indicator** as a lighter first step for the same gap.
7. **Off switch.** A config value turns the whole thing off. Default on.

## Suggested phases

1. Typing indicator plus a "working, N min" line for Admin's live chat.
2. Step labels from the Admin transcript.
3. Step labels for dispatcher bot runs through the streaming output format.

## Open questions for the Owner

1. Silent status message, or one that notifies? (My recommendation: silent.)
2. Keep a trail of steps after the reply, or delete the status when done?
3. Is the fixed label vocabulary specific enough, or do you want file *types*
   ("Reading documents")?

## What is left to the builder

Where the watcher runs (inside the dispatcher or as its own small unit), how
it finds the right transcript, the exact label table, and the edit-rate
numbers once the Telegram limits are checked against the current API docs.

## Constraints

- Python standard library only. No new model calls, anywhere, for this.
- Telegram token handling stays as it is: the stored secret, read only when
  sending, never logged.
- Tests: label mapping, rate limiting, tolerant parsing of malformed
  transcript lines, and the "status code fails silently" rule.
- No private names in any file, test or commit message.

## Done when

- A request that takes a minute or more shows a live status in Telegram, then
  a normal reply, with no model usage attributable to the status.
- Turning the setting off restores today's behaviour exactly.
- A broken transcript line or a failed Telegram call does not affect the work.
