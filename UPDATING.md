# Updating AtoBots

This guide is for a server that already runs AtoBots. There is one flow for
every install: look first, then start one job that does the rest and undoes
itself if it fails.

## Which case are you in?

Find the version first: `cb version` (an install older than 0.0.3 may not have
that command; look at `__version__` in the `__init__.py` under `engine/`).

| The install is | Do this |
|---|---|
| 0.1.0 or newer, with Git history | The short version below. |
| a copy with no Git history, 0.1.0 or newer | "A copied install". |
| older than 0.1.0 (any kind) | "Installs older than 0.1.0": one command, in two steps done for you. |

## The short version

Ask Admin in Telegram to update, or run the same two commands yourself as the
bots' account:

```bash
cb update --check
```

```bash
cb update
```

The first command changes nothing. It says what is running, what the target
is, what would change, what restarts, and how long the chat is offline. The
second starts the update in its own job, so it keeps running while Admin
restarts.

## What the update job does

1. Checks that the engine folder has no local edits and finds the target.
2. Copies the company database to
   `$ATOBOTS_HOME/backups/pre-update-<old version>-<UTC date and time>.db`
   (a full, consistent copy made by SQLite, readable only by you). The five
   newest copies are kept.
3. Sends the Owner a Telegram message: what is being installed and for about
   how long the chat will be offline.
4. Holds the queue and waits for bot runs to finish, up to 30 minutes. With
   runs still active it stops there and changes nothing, unless you passed
   `--force`.
5. Stops the dispatcher and the backup and health-check timers, and waits for
   a backup or health check that is already running.
6. Switches the code to the new version. From here on the new version runs
   its own steps: `cb init` (database changes; safe to repeat), the release's
   migration scripts, `cb templates refresh`, `cb bot relink`, the `cb`
   links, and the unit files.
7. Starts the dispatcher and the timers and, if it is enabled, restarts the
   always-on Admin (the conversation starts fresh).
8. Checks that the services came back and stayed up, then runs `cb doctor`.

Admin confirms to the Owner in Telegram when it is back. If Admin is not
enabled, the job sends the result itself.

## If something goes wrong

**Admin always comes back.** The job runs as its own systemd unit
(`atobots-update`, or `atobots-adopt` for `adopt.sh --detach`), so stopping
Admin does not stop it. When the job ends, however it ends (done, failed,
killed, or past its three-hour limit), systemd runs a small guard script
kept next to the log. If Admin or the backup and health-check timers are
enabled but not running, the guard starts them again and tells the Owner on
Telegram. It leaves the dispatcher stopped when the job ended half way,
because the code and the database may not match; the message says so, and
Admin can read the log and run `cb doctor` with you. After a normal update
the guard finds everything running and does nothing.

**The update rolls itself back.** When a step after the switch fails, or the
services do not come back, the job puts back the old code, the database copy
from step 2 and the old unit files, starts the services again, checks them,
and sends the Owner a message saying what failed and that it was rolled back.
Anything recorded in the database during the update itself is lost with the
restore; the queue is held for that time, so this is little.

**The rollback is tried once.** If it fails too, the job stops and sends a
message that says so. The steps to go back by hand are printed at the end of
the update log (`$ATOBOTS_HOME/logs/update-<time>.log`):

```bash
sudo systemctl stop atobots-dispatcher
```

```bash
git -C ~/AtoBots checkout --force --detach v0.1.0   # the version you were on
```

```bash
rm -f "$ATOBOTS_HOME/company.db-wal" "$ATOBOTS_HOME/company.db-shm"
```

```bash
cp "$ATOBOTS_HOME/backups/pre-update-<the file the log names>.db" "$ATOBOTS_HOME/company.db"
```

```bash
sudo systemctl start atobots-dispatcher
```

```bash
cb doctor
```

If Admin is enabled, start `atobots-admin` the same way.

**Admin is up but silent.** After a restart Claude Code can ask whether it
trusts the folder, and only someone at the server can answer. The finish
message says so when this happens. Fix it on the server:

```bash
tmux attach -t admin-live
```

Choose "Yes, I trust this folder", then detach with Ctrl-b d. The engine
never types into that question for you.

**`cb doctor` lists problems.** The finish message keeps these apart from the
update: "Not caused by the update: N existing issue(s) need a decision". They
were there before. Read each line and decide; common ones are a backup remote
that is not set up yet, or low disk space.

## When the update refuses to start

It stops before changing anything if:

- the engine folder has local edits (`git status` shows them; commit or
  discard them);
- the tag does not exist (`git -C ~/AtoBots tag --list 'v*'`);
- the install is a copy with no Git history (see the next section);
- bot runs are still active after 30 minutes (run it again later, or with
  `--force`).

## A copied install

Older installs were sometimes made by copying the engine files to the server.
`cb update --check` reports this as "a copy with no Git history". The copy is
turned into a Git clone once, by one job, with the same backup and the same
way back:

```bash
cb update --adopt --repo <repository-url>
```

It moves the old engine folder to `<folder>.old`, puts a clone in its place,
and finishes like any update. The old folder is kept until you remove it; the
finish message gives the command. If the adoption fails, the old folder and
the database are put back.

An install too old to have `cb update --adopt` is adopted by hand, once. Get
the new version onto the server and run its own script as the bots' account:

```bash
git clone <repository-url> ~/AtoBots.new
```

```bash
~/AtoBots.new/engine/adopt.sh
```

It finds the installed engine from the `cb` link in `~/.local/bin` (pass
`--old-engine PATH` if it cannot). `~/AtoBots.new` can be removed afterwards.

## Installs older than 0.1.0

0.1.0 renamed the services, the settings and the engine package, and later
releases no longer carry the code that moves an older install across. So an
older install cannot be sent straight to the newest release. The adopt script
does it in two steps by itself: it moves the install to release v0.1.0 first
(that release still knows the old layout), then to the release you asked for.
Each step has its own backup and undoes itself if it fails. The version that
sets this limit is in `engine/MIN_UPDATE_FROM`.

Use it for any install below 0.1.0, whether or not it has Git history. It
works from the newest release, so get that onto the server and run its script
as the bots' account. `--detach` runs it as its own job, so it keeps going
when Admin, the chat, is stopped halfway (do this if you started it from the
chat):

```bash
git clone <repository-url> ~/AtoBots.new
```

```bash
~/AtoBots.new/engine/adopt.sh --repo <repository-url> --detach
```

It prints the log file to watch. The chat is offline for a few minutes at each
step. Afterwards remove `~/AtoBots.new` and the old folder it names.

To have Admin do it from Telegram (its skills are from the old version, so
give it the steps), send something like:

> Update AtoBots with the new adopt script. Clone <repository-url> into
> ~/AtoBots.new, then run `~/AtoBots.new/engine/adopt.sh --repo
> <repository-url> --detach` and tell me the log file it prints. Do not stop
> or restart any service yourself; the job does that, and you will be
> offline for a few minutes.

The job sends its own Telegram messages, and the guard starts Admin again
whatever happens.

Two things that do not work, and the update tells you why:

- `cb update` from a 0.0.x install straight to a release past 0.1.0: the new
  version refuses before changing anything and rolls back with a message
  that names v0.1.0. Use the command above, or go through `cb update v0.1.0`
  first by hand and then `cb update`.
- Adopting from a zip (`--source`) when the install is older than 0.1.0: the
  steps in between have to be fetched, so `--repo` is required.

A fresh install with the company restored from a backup is not needed. (A
backup never holds secrets, so that route means re-entering them.)

## One specific release, or going back

```bash
cb update --check v0.1.0
```

```bash
cb update v0.1.0
```

### Roll back on purpose

Naming an older tag goes back to it; `--check` says "OLDER than what is
running" first. Read that release's section in [CHANGELOG.md](CHANGELOG.md)
before you do. A release older than 0.0.3 cannot finish its own update: use
the hand steps under "If something goes wrong" with that tag, and restore the database copy only if the older release cannot
open the current one.

## What an update never changes

Two things an update never changes for you, because they belong to the
company:

- A bot's Soul and persona. When a release changes a starting persona the
  CHANGELOG says so; compare with
  `diff "$ATOBOTS_HOME/bots/admin/PERSONA.md" engine/templates/admin/PERSONA.md`
  and take it with `cb bot set admin --persona-file engine/templates/admin/PERSONA.md`.
- A `rules.md` the company has edited. The new template is written beside it
  as `rules.md.new`, and `cb doctor` reports `rules-new` until you have
  compared them and removed that file.

Memory files, company work, secrets and `config.json` are not touched.
Bundled skills (`cb-basics`, `work-tracking`, `create-bot`) are replaced;
skills you added under other names stay.

## What each release tells you

Every release has a section in [CHANGELOG.md](CHANGELOG.md), in plain words.
A step that needs you is listed under "Manual steps after updating".

## Verifying releases

Maintainers sign a release with `git tag -s`. To make this clone refuse an
unsigned or wrongly signed tag, set the fingerprint `git verify-tag` prints:

```bash
git config atobots.releaseKey <fingerprint>
```

The update then runs `git verify-tag` and continues only when that
fingerprint is in the output. With no key configured it prints one note and
continues. `cb update --check` says which of the two applies.

## For maintainers: installing a build that is not a release

A zip or a folder holding the engine tree can be installed without a tag. It
is an **unverified local build**: nothing proves who made the file, and it
runs with the bots' account. It is installed only on the Owner's direct yes.

```bash
cb update --check --zip build.zip
```

```bash
cb update --zip build.zip --change C-0007
```

- The zip is unpacked by the installed version, in a temporary folder under
  the company home. It is refused if it holds an absolute path, a `..` path,
  a link of any kind, or no `engine/update.sh`.
- It is committed to a `local-builds` branch of the clone and installed like
  a tag, with the same backup and rollback.
- `cb version` then prints `<version>+local.<hash>`, so a local build is
  never mistaken for the release with the same number. The database records
  where the running engine came from (`cb update --check` shows it).
- A bot must name an approved change plan (`cb change plan`, then
  `cb change approve` with the Owner's exact words). A person at the server
  does not need one.
- Moving to a release later is allowed; `--check` says the local build will
  be replaced.

## For maintainers: migration scripts

A release that must change something on existing installs ships a script in
[engine/migrations](engine/migrations/README.md). The update runs the ones
between the old and the new version, in order. A failing script rolls the
update back.
