# Specs E, F, G — Updates from the first customer feedback

Source: Admin's ranked improvement list, the Owner's five change-safety
issues, the `python: not found` report and Admin's own notes on a lost
handoff, all from 2026-11-10. This document specifies the work. Nothing here
is built yet.

Specs A to D (the review fixes) are separate and stay as they are. These
three come after them.

| Spec | Theme | Release | Why this order |
|------|-------|---------|----------------|
| E | Safe changes to the live install | 0.0.3, with A to D | A bot stopped the service it runs in. That can take the company offline with nobody to bring it back. |
| F | Close the loop | 0.0.4 | Requests and replies get lost without the Owner noticing. |
| G | New abilities | 0.0.5 | Voice, encrypted file backup, better rollover. Useful, not urgent. |

## What the code already does

Read before building. Three of the reported gaps are partly closed in 0.0.2,
so the work is smaller than the feedback suggests.

- **Rollover is already automatic.** `rotation.py` restarts a session at
  `chat_rollover_tokens` (100000), or at `idle_rotate_tokens` (80000) once it
  has been idle longer than `cache_ttl_seconds` (300). The summary comes from
  the database with no model call, so it holds no conversation text. That is
  the real gap (task G3).
- **Admin is already told about queued messages.** With the always-on Admin
  up, `dispatcher._surface_admin` types `N message(s) are waiting for Admin.
  Run: cb inbox` into its session. The notice does not say who wrote, does not
  ask Admin to tell the Owner, and has no backstop if Admin ignores it
  (task F2). This arrived in 0.0.2; a server still on 0.0.1 has none of it.
- **The health check never restarts a service.** `doctor.py` only reports.
  For a down dispatcher it writes a local alert file and sends Telegram
  directly. The only automatic restart in the engine is systemd's
  `Restart=always` after a crash. The restart the Owner saw came from a bot
  with sudo acting on a finding whose text says "fix only what is safe"
  (task E4).
- **No engine file calls bare `python`.** Every call is `python3`. The
  failing hook is on the server, outside this repository (task E5).

## Rules for the builder

Same as Specs A to D: Python standard library only, Bash scripts pass
`shellcheck`, one task is one commit, every Python change has unit tests in
the named file, no company, person or server name anywhere, and no step
touches SSH, the firewall or the sudo rules. No server testing is part of
these specs; each task lists what the Owner checks on the server afterwards.

---

# Spec E — Safe changes to the live install

## Goal

A bot can never leave the company's services stopped, and no system change
happens without a written plan the Owner approved in their own words.

## Anti-goals

- The health check must not get quieter. Alerts stay; only self-repair of
  services goes.
- No new technical lock on sudo. All bots still share one account with sudo
  (review finding S1, out of scope here). These are working rules that the
  record backs up, like the rest of `rules.md`.

## Task E1 — An update that cannot kill its caller

Files: `engine/update.sh`, new `engine/claudebot/update.py`, `cli.py`.
Tests: `tests/test_update.py` (new), text checks in `tests/test_templates.py`.

Problem: `update.sh` stops the dispatcher and restarts `claudebot-admin`.
When Admin runs it from inside its own service, the restart kills the script
before it finishes, and a plain `systemctl stop` leaves nothing running.

1. New command `cb update TAG [--force]`. It starts the script as its own
   transient unit and returns at once:
   `sudo systemd-run --unit=claudebot-update --collect --no-block
   --setenv=CLAUDEBOT_HOME=... <engine>/update.sh TAG`.
   Output goes to `<home>/logs/update-<UTC time>.log` (a new folder, mode
   700, kept out of the Git backup). The command prints the
   log path and "the update runs on its own; Admin will restart at the end".
2. `cb update` refuses when a `claudebot-update` unit is already active.
3. `update.sh` refuses to run inside the Admin service. It reads
   `/proc/self/cgroup`; if the line names `claudebot-admin.service` it exits 1
   with "run `cb update TAG` instead". A person at the server (SSH shell, not
   in that cgroup) may still run the script directly.
4. `on_error` starts the services again when the update failed before the
   checkout moved (`CHANGED=0`), so a refused or early-failed update never
   leaves the dispatcher stopped. After the checkout moved it keeps printing
   the rollback steps as today, and adds a direct Telegram notice (E2).
5. The Admin restart stays the last service step and stays a `restart`,
   never a `stop` followed by a `start`.

Owner check on the server: run `cb update` from Telegram through Admin, see
Admin come back by itself and the log file complete.

## Task E2 — Prove the change worked before saying so

Files: `engine/update.sh`, `engine/claudebot/admin_live.py`. Tests:
`tests/test_admin_live.py`, `tests/test_templates.py`.

1. Before restarting Admin, `update.sh` writes `update-pending.json` in
   Admin's state folder (`admin_live.state_dir`, `<home>/.admin-live`) with
   the old version, new version and time.
2. `cb admin run`, once the new claude session is up and the marker exists,
   sends a direct Telegram message (`notify_telegram`, no model call):
   "ClaudeBot updated OLD to NEW. Admin is back." Then it removes the marker.
3. `update.sh` waits up to 180 seconds for both: `systemctl is-active` true
   for the dispatcher and (if enabled) Admin, and the marker gone. It then
   prints one line, PASS or FAIL, per check.
4. On FAIL it sends Telegram directly: which check failed and the log path.
   The update is reported done only when every check passed.
5. New command `cb verify services` runs the same two checks on demand, for
   use after any other system change. It exits 0 only when all pass.

Builder's call: the "Telegram reply from Admin" the Owner asked for is the
engine's own message from the Admin supervisor, not a model reply. It proves
the service and the Telegram path work without spending usage. If the Owner
wants a real model reply as proof, that is a small addition; see Decisions.

## Task E3 — A written, approved plan for every system change

Files: new `engine/claudebot/changes.py`, `db.py` (table `changes`, schema
version +1), `activity.py` (`cb log sudo`). Tests: `tests/test_changes.py`.

1. `cb change plan "TITLE" --steps-file FILE --touches PATH_OR_UNIT...`
   creates change `C-0001` with status `planned`. `--touches` is required and
   lists every folder, file, unit or package the change affects.
2. `cb change show C-1` prints the plan and the touch list, short enough to
   send by Telegram.
3. `cb change approve C-1 --quote "<the Owner's exact words>"` needs an Owner
   session, the same rule as `cb decision record` (Spec A's `Caller`). It
   stores the quote, the time and the session.
4. `cb change done C-1` refuses unless `cb verify services` passes at that
   moment; `--failed "what happened"` closes it as failed instead.
5. `cb log sudo` takes `--change C-N`. When the command it records contains
   `systemctl stop|restart|disable|mask`, `rm`, `mv`, `apt`, `pip`, `npm` or
   `install`, and no approved, open change is named, it records a
   `change.unplanned` activity line and prints a warning. It does not block:
   the record is the control, as with every other rule.
6. `cb doctor` reports changes left `approved` for more than 24 hours with
   no `done`.

## Task E4 — Alerts, not repairs

Files: `engine/claudebot/doctor.py`, `engine/templates/rules.md`,
`engine/templates/backend/SOUL.md`, `backend/PERSONA.md`,
`admin/PERSONA.md`, `skills/cb-basics/SKILL.md`. Tests:
`tests/test_doctor.py`, `tests/test_templates.py`.

1. Health-check messages to `backend` replace "fix only what is safe" with:
   "Do not stop, restart, install, move or delete anything. Report what you
   see to Admin. Safe repairs are: retry a backup push, clear a stuck run."
2. A down dispatcher or Admin is reported to the Owner (local file plus
   Telegram, as today) with the exact command for the Owner to run. No bot
   is asked to restart it.
3. `rules.md`, section "The server", gains these rules, in this wording:
   - Never stop or restart the service you run inside. A change to the live
     install runs on its own (`cb update`) and ends with a restart, never a
     stop.
   - Before any system change (stop, restart, move, delete, install), write
     the plan with `cb change plan`, list everything it touches, and get the
     Owner's yes recorded with `cb change approve`.
   - Never move or delete a folder that is not on the plan's touch list.
   - After a change, run `cb verify services`. Do not report it done until
     it passes.
   - The health check alerts. It never restarts a service.
4. Backend's Soul gets the first and last of those under "What I will never
   do". Souls are Owner-only, so an existing Soul is not changed by the
   update: confirm `cb templates refresh` leaves it alone (add a test if none
   exists), and list the Soul edit in UPDATING.md as a manual Owner step.
5. `cb log sudo` records free text today ("what and why"). E3 step 5 matches
   its words against that text; keep the match simple and whole-word.

## Task E5 — Find and prevent the `python: not found` hook

Files: `engine/claudebot/doctor.py`, `tests/test_doctor.py`,
`tests/test_templates.py`.

The engine has no bare `python` call, so the broken hook was added on the
server (a settings file in the company home or a bot folder, a plugin, or a
routine check).

1. New health check `commands`: read every hook command in
   `.claude/settings.json` and `.claude/settings.local.json` under the
   company home and each bot folder, plus every routine check command. Take
   the first word of each. If it is not an absolute path that exists and is
   not found on `PATH`, report one finding naming the file and the word, with
   the hint "this server has python3, not python" when the word is `python`.
2. A test in `tests/test_templates.py` fails if any engine script, unit or
   template calls bare `python`.
3. UPDATING.md gets one line for existing installs: run `cb doctor`, then
   change `python` to `python3` in the file it names.

Not doing: installing a `python` alias package. That is a system change the
Owner can choose instead (see Decisions).

---

# Spec F — Close the loop

## Goal

No Owner request sits unlinked without being seen, no bot says "sent" before
it is true, and a reply to Admin reaches the Owner without them asking.

## Anti-goal

Never link a request to an item on a guess. A linked request leaves the
"unhandled" list, so a wrong link hides the Owner's words. That would be
worse than today's manual step.

## Task F1 — A send that is plainly sent or not sent

Files: `engine/claudebot/messages.py`, `cli.py`,
`skills/cb-basics/SKILL.md`. Tests: `tests/test_messages.py`.

1. On success `cb send`, `cb ask` and `cb reply` keep printing the message id
   on stdout and add one line on stderr: `Sent to NAME as message N.`
2. A usage error on these three prints one line before the usage text:
   `Not sent. Example: cb send tl "subject" "body"`, and exits 2 as today.
3. `cb-basics` adds: "A message is sent only when the command prints an id.
   Any other result means it was not sent. Do not tell the Owner it was
   passed on until you have the id."

## Task F2 — A reply to Admin reaches the Owner

Files: `engine/claudebot/dispatcher.py`, `admin_live.py` (`LIVE_PROMPT`,
`surface`), `config.py`. Tests: `tests/test_dispatcher.py`,
`tests/test_admin_live.py`.

1. The notice typed into the always-on Admin names the senders:
   `2 message(s) are waiting for Admin, from tl, backend. Read them with cb
   inbox and tell the Owner what matters.` Only bot names and counts go in
   the text. Bot names are already limited to safe characters; subjects and
   bodies are never typed into the terminal (Spec A, task A4).
2. `LIVE_PROMPT` gains a rule: a line starting `[system]` comes from the
   engine. On a "waiting for Admin" notice, read the inbox, and when a
   message answers something the Owner asked, send them the result on
   Telegram in the same turn.
3. Backstop with no model call: when a message to Admin from another bot is
   still not done `admin_relay_minutes` (default 15) after it was surfaced,
   the dispatcher sends Telegram directly: `tl answered Admin 15 minutes ago
   and Admin has not read it yet.` Once per message.
4. When the always-on Admin is not running, nothing changes: the dispatcher
   wakes Admin for the message as it does today.

## Task F3 — Requests link themselves when the link is certain

Files: `engine/claudebot/items.py`, `requests.py`, `search.py`,
`skills/work-tracking/SKILL.md`. Tests: `tests/test_items.py`,
`tests/test_requests.py`.

1. `cb item new`, run inside a session, links the new item to every unhandled
   request captured in that same session (`owner_requests.session_id`). It
   prints `Linked to R-12.` `--request R-N` picks one; `--no-request` links
   none.
2. `cb request unhandled` shows, under each request, up to three open items
   whose text matches it (the existing search index), as a ready command:
   `maybe: cb request link R-12 T-0014  (voice message transcription)`.
   It is a hint only. Nothing is linked on a text match.
3. New `cb request close R-N --reason "TEXT"` for a request that needs no
   item (a question answered in chat). It leaves the unhandled list and the
   reason is kept. Skip this step if the command already exists under another
   name.
4. The daily digest (F4) lists requests still unhandled after 24 hours.

## Task F4 — A daily "what changed" message

Files: new `engine/claudebot/digest.py`, `dispatcher.py`, `config.py`,
`cli.py`. Tests: `tests/test_digest.py`.

1. `cb digest` prints what changed since the last digest, built from the
   records with no model call: items closed, items opened, items waiting on
   the Owner, requests still unhandled, decisions recorded, changes made
   (E3), health findings, failed runs and usage for the period.
2. Config `digest_time` (for example `08:00`, company time zone; empty means
   off, which is the default). At that time the dispatcher sends the digest
   by Telegram directly and stores the time in `meta`.
3. No message on a day when nothing changed. One Telegram message at most,
   cut at 3500 characters with "more: cb digest".

---

# Spec G — New abilities

## Task G1 — Voice messages become text

Files: new `engine/claudebot/transcribe.py`, `config.py`, `doctor.py`,
`admin_live.py` (`LIVE_PROMPT`). Tests: `tests/test_transcribe.py`.

1. `cb transcribe FILE` runs the command in config `transcribe_cmd` with the
   file path as its last argument, under the same memory limit as a bot run
   and a 5 minute time limit, and prints the text. With no `transcribe_cmd`
   it exits 1 with "no speech-to-text tool is set up; ask the Owner".
2. The engine installs nothing. UPDATING.md documents one recommended local
   tool (whisper.cpp with a small multilingual model) as an Owner step.
3. `LIVE_PROMPT`: when a voice or audio file arrives, run `cb transcribe` on
   it, treat the text as the Owner's words only if the sender is on the allow
   list, and quote the text back in the reply so a mishearing is caught.
4. `cb doctor` reports a `transcribe_cmd` whose program is missing (covered
   by the E5 check once that command is added to its list).

To confirm before building: how the Telegram plugin hands a voice message to
the session (a file path, or nothing). If it gives no file, this task needs a
plugin change first and moves out of 0.0.5.

## Task G2 — Encrypted backup for files

Files: `engine/claudebot/backup.py`, `config.py`. Tests:
`tests/test_backup.py`.

1. `cb backup files add PATH` and `cb backup files list` keep a list of
   folders that hold private files and stay out of the normal Git backup.
2. `cb backup files now` makes a tar of each folder, encrypts it with
   `gpg --symmetric --cipher-algo AES256 --batch --passphrase-fd`, the
   passphrase read from the store item `backup-passphrase`, and pushes the
   result to the remote named `files`. The plain tar is never written
   outside a private temporary folder and is removed on every exit path.
3. `cb backup files verify` fetches the newest copy and decrypts it to
   nowhere, to prove the passphrase still works. The backup timer runs
   `now` then `verify` once the list is not empty.
4. `cb backup restore --files SOURCE TARGET` brings them back.
5. The store is never backed up, so the passphrase is not in any backup.
   The first `cb backup files add` prints, and the docs repeat: "Keep the
   backup passphrase somewhere off this server. Without it these backups
   cannot be read."

gpg is used because it is already on the server, so nothing is installed.

## Task G3 — A rollover that keeps the thread

Files: `engine/claudebot/rotation.py`, `chat.py`, `admin_live.py`. Tests:
`tests/test_rotation.py`.

1. When a session passes the rollover size and is safe to restart, the
   engine first types `[system] This session is long. Write your handoff now
   with cb chat-rollover.` and waits up to `rollover_handoff_seconds`
   (default 180). Admin's own summary is used when it arrives.
2. If none arrives, the engine rolls over with its record-built summary as
   today, plus the text of the last five Owner messages and Admin replies
   read from the transcript (no model call, each cut at 600 characters).
3. `cb config get chat_rollover_tokens` and the two related keys are listed
   in `cb-basics`, so Admin can say when rollover will happen.

---

## Decisions for the Owner

1. **Proof after a change (E2).** The spec uses an engine-sent Telegram
   message as proof. It costs nothing and works even if the model is down. A
   real reply written by Admin proves more but spends usage on every update.
   Recommended: engine message.
2. **How strict the change plan is (E3).** The spec warns and records when a
   risky sudo command has no approved plan. Blocking instead is possible, but
   a bot with sudo can go around `cb`, so it would only look stronger.
   Recommended: warn and record.
3. **The `python` hook (E5).** Fix the one hook to say `python3`
   (recommended), or install the system alias so `python` works everywhere.
4. **Speech-to-text (G1).** A local tool keeps audio on the server but needs
   about 1 GB of memory while it runs and an install the Owner approves. Which
   languages must it handle?
5. **Where encrypted files go (G2).** A Git remote suits small folders only;
   the backup already keeps files over 50 MB out. If the private files are
   large, the `files` remote should be storage reached with rsync instead.
   How big is the folder today?
6. **Daily digest time (F4).** Off by default. What time should it arrive?

## Not doing

- More bots. Admin's own advice, and correct: these basics come first.
- Narrowing sudo (finding S1). It is the real fix behind Spec E and has its
  own decision pending.
- Linking requests to items by text match (see Spec F's anti-goal).
- An AI-written digest. The record-built one is free and cannot invent
  progress.

## Where each piece of feedback went

| Feedback | Task |
|----------|------|
| Auto-link requests to items | F3 |
| Speech-to-text for voice messages | G1 |
| Encrypted file backup | G2 |
| Automatic context rollover | G3 (already automatic; the summary is the gap) |
| Daily "what changed" on Telegram | F4 |
| Never stop the service I run inside | E1, E4 |
| Plan, touch list and recorded yes before a change | E3, E4 |
| Check status and Telegram before reporting done | E2 |
| No moving or deleting unlisted folders | E3, E4 |
| Health check alerts, does not restart | E4 |
| `python: not found` in a hook | E5 |
| "Passed to tl" said before the send worked | F1 |
| `cb send --help` not read | F1 |
| Nobody watching for the reply; wake and relay | F2 |
