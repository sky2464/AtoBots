# Updating ClaudeBot

This guide is for a server that already runs ClaudeBot. There is one flow for
every install: look first, then start one job that does the rest and undoes
itself if it fails.

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
   `$CLAUDEBOT_HOME/backups/pre-update-<old version>-<UTC date and time>.db`
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

**The update rolls itself back.** When a step after the switch fails, or the
services do not come back, the job puts back the old code, the database copy
from step 2 and the old unit files, starts the services again, checks them,
and sends the Owner a message saying what failed and that it was rolled back.
Anything recorded in the database during the update itself is lost with the
restore; the queue is held for that time, so this is little.

**The rollback is tried once.** If it fails too, the job stops and sends a
message that says so. The steps to go back by hand are printed at the end of
the update log (`$CLAUDEBOT_HOME/logs/update-<time>.log`):

```bash
sudo systemctl stop claudebot-dispatcher
```

```bash
git -C ~/ClaudeBot checkout --force --detach v0.0.2   # the version you were on
```

```bash
rm -f "$CLAUDEBOT_HOME/company.db-wal" "$CLAUDEBOT_HOME/company.db-shm"
```

```bash
cp "$CLAUDEBOT_HOME/backups/pre-update-<the file the log names>.db" "$CLAUDEBOT_HOME/company.db"
```

```bash
sudo systemctl start claudebot-dispatcher
```

```bash
cb doctor
```

If Admin is enabled, start `claudebot-admin` the same way.

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
- the tag does not exist (`git -C ~/ClaudeBot tag --list 'v*'`);
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
git clone <repository-url> ~/ClaudeBot.new
```

```bash
~/ClaudeBot.new/engine/adopt.sh
```

It finds the installed engine from the `cb` link in `~/.local/bin` (pass
`--old-engine PATH` if it cannot). `~/ClaudeBot.new` can be removed afterwards.

## One specific release, or going back

```bash
cb update --check v0.0.3
```

```bash
cb update v0.0.3
```

### Roll back on purpose

Naming an older tag goes back to it; `--check` says "OLDER than what is
running" first. Read that release's section in [CHANGELOG.md](CHANGELOG.md)
before you do. A release older than 0.0.3 cannot finish its own update, so
the updater refuses it: use the hand steps under "If something goes wrong"
with that tag, and restore the database copy only if the older release cannot
open the current one.

## Updating from 0.0.2 or earlier

Those versions carry the old updater, which knows nothing about the steps
above. Take them to **0.0.3 first, and name the tag** (the old updater would
otherwise pick the newest release, and a release after 0.0.3 refuses to be
installed by it). Run it the old way, as the bots' account, in the engine
folder:

```bash
engine/update.sh --tag v0.0.3
```

A build of 0.0.2 made from `main` hands the rest of the update to the new
version, which finishes as described above. The 0.0.2 and 0.0.1 releases
cannot hand over; after their updater ends, finish by hand (use the same
company name and company home as the first install; this does not change
company data):

```bash
engine/install.sh --start "My Company" "$CLAUDEBOT_HOME"
```

```bash
cb templates refresh && cb bot relink
```

```bash
sudo systemctl restart claudebot-admin   # only if the always-on Admin is enabled
```

```bash
cb version && cb doctor
```

From 0.0.3 on, use `cb update`.

Two things an update never changes for you, because they belong to the
company:

- A bot's Soul and persona. When a release changes a starting persona the
  CHANGELOG says so; compare with
  `diff "$CLAUDEBOT_HOME/bots/admin/PERSONA.md" engine/templates/admin/PERSONA.md`
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
git config claudebot.releaseKey <fingerprint>
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
  a link that points outside, or no `engine/update.sh`.
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
