# Smooth updates — design

Status: **BUILT in 0.0.3.** What follows is the design as it was written
before the build. How to use the result: [UPDATING.md](../../../UPDATING.md).
The project that waited for this one:
[Telegram status peek](2026-11-10-telegram-status-peek-design.md).

## Why

The safe-update work (Spec C) made `engine/update.sh` careful: it backs up the
database, holds the queue, waits for runs to finish, re-renders units and
checks the result. But moving a live install to a new version is still not
smooth. The last real attempt went like this:

- The Owner sent a zip of the newest engine through Telegram and said "replace
  the old packages with this and update your environment".
- Admin filed the zip, backed up the database, then found two blockers: the
  live engine folder is a copied folder with no `.git`, and `update.sh` can
  only install from a release tag, while the zip was **newer** than the newest
  tag.
- Admin stopped and asked the Owner to choose between two workarounds. Nothing
  was broken, but nothing was updated either, and Admin spent a long turn
  working all of this out by reading files.

The Owner's picture of "smooth": say "update" in Telegram, and a few minutes
later hear that it is done — whether the new version is an official release or
a build the Owner made, and whatever way the install was first made.

## Goals

1. **One flow for every install.** A copied install (no `.git`) is turned into
   a proper clone by the tooling, once, with a backup and a way back, instead
   of by a manual eight-step recipe.
2. **Two sources, one path.** The new version may be a release tag or a local
   build (zip or folder). Both go through the same backup, hold, switch,
   verify and rollback steps.
3. **Look before you leap.** One read-only command tells Admin (or the Owner)
   exactly what an update would do, so nobody has to read the new files to
   find out.
4. **No silent surprises.** The Owner is told before the chat goes offline,
   for how long, and what comes back afterwards.
5. **Re-running the installer or updater never damages the engine.** No step
   writes through a symlink into the clone, no job is left half-run, and the
   Owner is not left with a silent Admin (the problems listed above).
6. **A hook for migrations.** A version can ship a small ordered script that
   runs during the update, so later changes (the rename in project 2 above
   all) can move existing installs without a manual recipe.

## Non-goals

- Updating without the Owner's yes. An update never starts by itself.
- Touching SSH, the firewall, sudo rules or secrets. The engine's standing
  rules apply: never lock the Owner out.
- A new installer, a package manager, Docker or a hosted update service.
- Replacing Git as the engine's version store (see "Considered and left out").
- Making the update itself faster than a few minutes. Safety first.

## Anti-goals (a failure even if it technically works)

- An update that leaves the Owner with the dispatcher held, Admin down and no
  message saying so.
- A local build that can be installed without the Owner's explicit yes, or that
  quietly replaces an official release and cannot be told apart from it.
- Any new manual step that only works if someone is at a terminal while Admin
  is the thing being restarted.

## What already exists (keep, do not redo)

`engine/update.sh`, `engine/atobots/update.py` (`cb update TAG` starts the
script in a standalone systemd unit so it survives Admin's restart),
`engine/lib/units.sh`, the schema-version refusal, `cb dispatcher hold` /
`release` / `idle`, `cb templates refresh` with the `rules.md` protection, the
`update-pending` marker that makes Admin confirm after its restart, the
rollback instructions printed on failure, and `UPDATING.md`. Read them first;
this design extends them.

## Problems seen in the first real update (must be fixed here)

The Owner's server then ran the manual conversion (a copied install turned
into a clone at 0.0.2). It finished, but it exposed more update bugs. This
list comes from the session's own report, and the pasted terminal text was
cut off partway through the last item, so each needs confirming in the code:

1. **The installer can overwrite the engine's own `cb` script.** On an older
   install `/usr/local/bin/cb` was a root-owned symlink pointing at
   `engine/bin/cb`. `engine/install.sh` (around line 91) writes the new
   wrapper with `sudo tee /usr/local/bin/cb`, and `tee` follows the symlink,
   so it wrote the wrapper *over the repository's `engine/bin/cb`*. The wrapper
   then runs `engine/bin/cb`, which is itself. The tree also becomes dirty,
   which blocks the next update. The session had to remove the symlink and
   restore the file from Git by hand. Fix: remove any existing
   `/usr/local/bin/cb` (file or symlink) first, then install the wrapper from
   a temp file with `sudo install -m 755`, and never write through a symlink.
   Add the same guard to any other file the installer writes under `/usr` or
   `/etc`.
2. **A migration job wedged, and two jobs were left running.** The session
   stopped "the wedged migration job" and two orphaned `cb` jobs
   (`doctor --notify` and `backup now`). Updates and installs must not start
   (or must wait for, or stop cleanly) the timer-driven jobs while the
   engine is being switched, and a hung step needs a timeout and a clear
   message instead of waiting forever. Find out which step wedged; the first
   place to look is the 30-minute idle wait and the units that run during it.
3. **Admin comes back stuck on Claude Code's folder-trust question.** After
   the switch Admin was waiting for "Yes, I trust this folder", which only
   someone attached to the `admin-live` tmux session can answer, so Telegram
   stays silent. The installer only prints a note about it. The updater must
   detect "Admin is up but not answering" and say so in the finish message,
   with the exact one-line fix. Pre-answering the trust question safely (so a
   new engine folder is already trusted) should be investigated; it must not
   weaken what the trust question protects, so if there is no safe way, keep
   the detection and the message.
4. **Doctor findings arrive at the worst moment.** The session reported stale
   task leases with no live run, bots with no worker set, and a missing
   `timezone` key. These are not update failures, but the update's finish
   message should separate "the update worked" from "these existing issues need
   a decision", and list the decisions in plain words once.

## Design

### 1. Adopting a copied install

A new command `cb update --adopt SOURCE` (or an `engine/adopt.sh` the Owner
can run from a terminal; builder's choice) does what the current "Moving a
copied install to a clone" section does by hand: back up the database, hold
and stop the services, move the old engine folder to `<folder>.old`, bring in
the new engine **as a Git clone**, run the installer over the existing company
home, refresh templates, relink, restart, and run `cb doctor`.

`SOURCE` is a repository address plus tag, or a zip or folder (section 2).
Everything runs as one detached job (it stops Admin, so it cannot depend on
Admin). The old folder is kept until the Owner removes it; the rollback is
printed, and it is also written to the update log.

The detection is automatic: when `update.sh` finds no Git clone it stops
with "this install is a copy; run `cb update --adopt ...`" and the same
message is what `--check` reports.

### 2. Local builds

A local build is a zip or folder containing the engine tree. It is imported
into the clone, then installed like a tag.

- **Import** is done by the *currently installed* code, never by a script that
  came inside the zip. The zip is extracted into a fresh temp folder under the
  company home (never inside the clone), and refused if it has absolute paths,
  `..` segments, symlinks that point outside, or no `engine/update.sh`.
- The tree is committed to a branch `local-builds` in the clone, with the
  message `local build <sha256-first-12> from <file name>`. The clone is then
  checked out on that commit, like a tag.
- The reported version becomes `<version>+local.<first 7 of the sha>`, so a
  local build can never be mistaken for the official release with the same
  number. `cb version`, `cb doctor` and the update messages all show it.
- The company database records where the running engine came from
  (`meta.engine_source`: `tag:v0.0.2` or `local:<sha>`), so "what is running?"
  has a one-line answer.
- Leaving a local build for a tag later is allowed. `--check` shows what would
  be lost (the diff between the two) and the update says so before it starts.
- The zip came through the chat, so the existing rule applies: it is acted on
  only after the Owner's direct yes to Admin, recorded the usual way. The
  files being installed will run with the bots' account, which has sudo; the
  design does not pretend a zip is verified, and the check output says
  "unverified local build".

### 3. `cb update --check`

Read-only and quick. Takes the same source arguments as an update (a tag, the
newest tag, `--zip FILE`, `--dir FOLDER`) and prints, in plain words and as
`--json`:

- what is running now, and where it came from;
- the target, and whether it is newer, the same, or older;
- how many files differ, and whether any template, unit file or database
  schema change is included;
- whether this install must be adopted first (no `.git`);
- whether the working tree has local edits;
- whether bot runs are active right now;
- what will restart, and the expected time the chat is offline (measured from
  the previous update's log when there is one; "about 2 minutes" otherwise).

Admin's persona and `rules.md` are updated so that "update" starts with this
command and the reply to the Owner is built from its output. Admin does not
read the new files to decide.

### 4. Telling the Owner

- Before anything stops: the update job sends one Telegram message through the
  Bot API directly (the same path `cb admin notify` uses), for example
  "Updating from X to Y now. The chat will be offline for about N minutes."
- After: the existing flow stays (Admin confirms after restart through the
  marker; the job sends a failure notice itself). If Admin is not enabled the
  job sends the success notice as well.

### 5. Migrations

`engine/migrations/` holds scripts named
`<version>-<short-name>.sh`, for example `0.1.0-example.sh`. During an update,
after the checkout and `cb init`, the updater runs every script whose version
is greater than the old version and not greater than the new one, in order.
Rules for these scripts: idempotent, bash with `set -euo pipefail`, no
SSH/firewall/sudoers/secrets, and their output goes to the update log. A
failing migration takes the normal failure path (notice, rollback steps).
Version ordering must ignore the `+local...` suffix.

## Considered and left out

- **A `releases/<version>/` folder plus a `current` link, with no Git on the
  server.** Cleaner rollback and no "dirty tree" problem, and it would also
  fix the stale links into a moved folder. Left out for now: it rewrites the
  installer, the units, `update.sh`, the tests and the docs while a live
  install depends on them, and Git already gives this design a version store
  and a rollback. It can be revisited when project 2 renames the install
  folder, because that is a natural moment to change layout.
- **Automatic updates.** Left out on purpose (non-goal above).

## Decisions by the Owner

1. **Automatic rollback: yes.** If the services do not come back healthy, the
   update job rolls back the code to the old ref and restores the pre-update
   database itself, restarts the services, verifies them, then sends the
   failure notice through the Bot API (Admin may be down). A restore discards
   anything written after the backup; the queue is held during the update, so
   little should be lost. The notice says plainly what was rolled back. The
   printed manual steps stay as the fallback when the automatic rollback
   itself fails. The builder must cover a failed rollback (do not loop; stop
   and report).
2. **Local builds: an Owner-only convenience.** They are built and documented
   for maintainers, with the "unverified local build" warning, but the public
   docs do not advertise them. The code path is the same as for tags.

## My calls (the Owner has not seen these)

- Keep Git as the version store; do not move to a releases folder (above).
- Import local builds on a `local-builds` branch with the `+local.<sha>`
  version suffix.
- Run the import with the installed code, not the zip's.
- Start notice from the job; success notice from Admin (the job only sends it
  when Admin is not enabled).
- Migration scripts live in `engine/migrations/`, one per version.

## What is left to the builder

Command names and flags, the exact wording of messages, how `--check` measures
the expected offline time, how the shell and Python pieces divide the work
(`update.sh` is already 400+ lines; splitting it into `engine/lib/` helpers is
welcome if it keeps `shellcheck` clean), and the test seams.

## Constraints

- Bash scripts: `set -euo pipefail`, pass `shellcheck`. Engine code: Python
  standard library only.
- Tests: `python3 -m unittest discover -s tests` stays green. Python parts
  get unit tests (zip validation including the refused cases, version
  ordering with the `+local` suffix, the `--check` output on a copied install,
  a tag, a newer-than-tags local build and a dirty tree). Shell parts get text
  assertions in `tests/test_templates.py`, plus one scripted run in a temp
  folder with a throwaway Git repository and a fake company home that skips
  `systemctl` through an environment switch.
- No company, person, product or server name goes into any file, test or
  commit message.
- The existing safety rules in `CLAUDE.md` bind this work.
- No server testing is part of the build; the Owner runs the first real
  update afterwards.

## Done when

- The situation from the Telegram screenshot (a copied install and a zip newer
  than every tag) is handled by: `cb update --check --zip FILE` giving a
  one-paragraph answer, and one Owner yes leading to a finished, verified
  update with start and finish messages.
- Running `engine/install.sh` on a machine where `/usr/local/bin/cb` is a
  symlink into the clone leaves the clone clean (tested with a throwaway
  prefix instead of the real `/usr/local/bin`), and the repeat run is a no-op.
- After an update, if Admin is waiting on the folder-trust question or is
  otherwise not answering, the finish message says so and gives the fix.
- A deliberately failed update (tested with a failing migration or a service
  that will not start) rolls back the code and the database by itself and
  sends a notice saying so.
- `UPDATING.md` describes one flow, with the manual recipe reduced to a
  fallback.
- All new behaviour has tests; the suite and `shellcheck` pass.
