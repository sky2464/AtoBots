# Updating ClaudeBot

This guide is for customers who installed ClaudeBot from GitHub: you cloned the
repository on your server and ran `engine/install.sh`. The supported deploy path
is a Git clone plus `engine/update.sh` after release tags (see
[docs/deploy.md](docs/deploy.md)). Do not rsync with `--delete` into the clone.

Run every command below as the same UNIX account that runs the bots, in the
ClaudeBot clone (the folder that holds `engine/`).

## See the current version

```bash
cb version
```

## See the available releases

```bash
git fetch --tags && git tag --list 'v*' --sort=v:refname
```

The newest release is the last line. Release notes are also on the GitHub
Releases page of the repository.

## Update to the newest release

```bash
engine/update.sh
```

To update to one specific release instead:

```bash
engine/update.sh --tag v0.0.1
```

The script runs these steps in order and prints each one. It waits for
dispatcher runs to finish (up to 30 minutes), re-installs changed systemd unit
files, refreshes engine-owned `rules.md` and bundled skills (not bot SOUL or
PERSONA files), then restarts the dispatcher (and Admin, if it is enabled), so
running work pauses for a moment.

## What the update backs up, and where

Before anything changes, the script copies the company database to:

```
$CLAUDEBOT_HOME/backups/pre-update-<old version>-<UTC date>.db
```

For example, `$CLAUDEBOT_HOME/backups/pre-update-0.0.1-2026-11-10.db`.
The folder is created if it does not exist, and the file is readable only by
you. The copy is a full, consistent copy made by SQLite. The update runs
`cb init`, which also rewrites each bot's settings file and commits any pending
company changes to the company's Git history. Restoring the database backup
does not undo those.

## If the update refuses to start

The script stops before changing anything if:

- the ClaudeBot checkout has local changes. Look at them with `git status`, then
  commit or discard them and run the update again;
- the tag you asked for does not exist. Check the list in "See the available
  releases";
- there is no database yet. Run `engine/install.sh` first.

## If an update fails

The script prints the exact steps to go back. They are also below, with your
own values filled in by the script when it runs. In short:

```bash
sudo systemctl stop claudebot-dispatcher
```

Put the old code back. Use the old version tag or commit the script printed:

```bash
git checkout v0.0.1
```

Remove the database's temporary files, then restore the database from the backup
the script printed (replace the paths with yours):

```bash
rm -f /home/claude/MyCompany/company.db-wal /home/claude/MyCompany/company.db-shm
```

```bash
cp /home/claude/MyCompany/backups/pre-update-0.0.1-2026-11-10.db /home/claude/MyCompany/company.db
```

Start the services again:

```bash
sudo systemctl start claudebot-dispatcher
```

Then check the install:

```bash
cb doctor
```

If Admin is enabled, stop and start it as well (`claudebot-admin`), in the same
way as the dispatcher.

## Roll back on purpose

To go back to an older release after a successful update, use the same steps as
above with the older tag. Restore the database only if the older release cannot
open it. Read that release's section in `CHANGELOG.md` first.

## If `cb doctor` reports problems

`cb doctor` lists every problem it finds, one line each, and exits with an
error when there are any. The update itself has finished when this happens, and
nothing was rolled back. Read each line and fix it. Common cases:

- **The dispatcher is down.** Start it with `sudo systemctl start claudebot-dispatcher`.
- **Backups are not pushed to a remote yet.** This is expected until a backup
  remote is set up. It is not caused by the update.
- **Disk or memory is low.** Free space or stop other programs on the server.

Run `cb doctor` again after each fix. A line that is still there means the
problem is not fixed yet.

## What each release tells you

Every release has a section in [CHANGELOG.md](CHANGELOG.md). It lists the changes
in plain words. If a release needs a step from you (for example, copying a file
by hand), the section says so under "Manual steps after updating". Read that
section before you update.

## Things an update does not change

An update replaces the engine code. Apart from the bot settings files and the
Git commit described above, it does not rewrite your company's own files, so
your choices stay as they are:

- `rules.md` in the company home is not rewritten by an update.
- Each bot's persona is not rewritten by an update. A persona is changed only
  with the Owner's agreement.
- Memory files and company work are not touched.

When a release changes one of these starting files, the CHANGELOG says what to
copy by hand, and where the new version is in the engine's `engine/templates/`
folder.
