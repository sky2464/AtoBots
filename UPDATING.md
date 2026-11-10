# Updating ClaudeBot

This guide is for a server that already runs ClaudeBot. The supported setup is
a Git clone of this repository on the server, installed with
`engine/install.sh` and updated with `engine/update.sh`. Do not copy files into
the clone with rsync.

Run every command below as the same UNIX account that runs the bots, in the
ClaudeBot clone (the folder that holds `engine/`).

| Your server now | Go to |
|---|---|
| A Git clone on 0.0.1 | [Updating from 0.0.1 to 0.0.2](#updating-from-001-to-002) |
| Engine files copied by rsync (the folder has no `.git`) | [Moving a copied install to a clone](#moving-a-copied-install-to-a-clone) |
| A Git clone on 0.0.2 or later | [Update to the newest release](#update-to-the-newest-release) |

To tell which you have: `git -C ~/ClaudeBot rev-parse --is-inside-work-tree`
prints `true` for a clone (use your own path if the engine is elsewhere).

## Before you update

- Read the release's section in [CHANGELOG.md](CHANGELOG.md), above all
  "Manual steps after updating" and "Known issues".
- Pick a quiet moment. The update restarts the dispatcher, and it restarts the
  always-on Admin if that is enabled, which ends the Owner's current
  conversation with it. Work in the queue is kept.
- Close any open `cb chat` first.
- The server needs read access to the repository (`git fetch` must work).

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
engine/update.sh --tag v0.0.2
```

From 0.0.2 on, the script runs these steps in order and prints each one:

1. checks that the clone has no local changes and fetches the release tags;
2. copies the company database to a backup file (see below);
3. moves the clone to the release tag;
4. runs `cb init` (adds any new database columns; safe to repeat) and
   `cb templates refresh`. Bundled skills are replaced. `rules.md` is replaced
   only when it still matches a template this project shipped; if the company
   has edited it, that file is left alone and the new template is written
   beside it as `rules.md.new`. Bot Soul, Persona and Memory files are not
   touched;
5. waits for bot runs to finish, up to 30 minutes;
6. reinstalls the dispatcher, backup and health-check unit files and restarts
   the dispatcher, and Admin if it is enabled;
7. runs `cb doctor`.

It does not reinstall the always-on Admin's unit file. When a release says the
units changed, run `engine/install.sh` again afterwards (safe to repeat).

## Updating from 0.0.1 to 0.0.2

The updater that runs is the one already on your server (0.0.1's). It backs
up the database, moves to 0.0.2, runs `cb init` and restarts the services, but
it does not know the newer steps, so you finish them by hand.

1. See where you are. It should print `0.0.1`:

```bash
cb version
```

2. Run the update:

```bash
engine/update.sh --tag v0.0.2
```

3. Reinstall the unit files and links. Use the same company name and company
home as the first install; this does not change company data:

```bash
engine/install.sh --start "My Company" "$CLAUDEBOT_HOME"
```

4. If you ever edited `rules.md` in the company home, save your copy before
the next step replaces it:

```bash
cp "$CLAUDEBOT_HOME/rules.md" "$CLAUDEBOT_HOME/rules.md.mine"
```

5. Bring in this release's rules and bundled skills, and point every bot at
them:

```bash
cb templates refresh
```

```bash
cb bot relink
```

6. If the always-on Admin is enabled, restart it so it reads the new rules
(this ends the current conversation with it):

```bash
sudo systemctl restart claudebot-admin
```

7. Check the result. `cb version` should print `0.0.2`:

```bash
cb version
```

```bash
cb doctor
```

8. Admin's starting persona changed in this release. A persona is changed
only with the Owner's agreement, so the update leaves yours alone. Look at the
difference:

```bash
diff "$CLAUDEBOT_HOME/bots/admin/PERSONA.md" engine/templates/admin/PERSONA.md
```

To take the new one as it is:

```bash
cb bot set admin --persona-file engine/templates/admin/PERSONA.md
```

Optional settings new in 0.0.2 go in `$CLAUDEBOT_HOME/config.json`; see
"Configuration" in the [README](README.md). Nothing needs to be set for an
existing install.

## Moving a copied install to a clone

Older installs were sometimes made by copying the engine files to the server.
`engine/update.sh` needs a Git clone, so move to one once. Company data is in
the company home and is not touched; these steps replace only the engine
folder. The examples assume the engine is in `~/ClaudeBot`.

1. Stop the services (the second line only if the always-on Admin is enabled):

```bash
sudo systemctl stop claudebot-dispatcher
```

```bash
sudo systemctl stop claudebot-admin
```

2. Back up the database:

```bash
mkdir -p "$CLAUDEBOT_HOME/backups" && python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$CLAUDEBOT_HOME/company.db" "$CLAUDEBOT_HOME/backups/pre-clone-move.db"
```

3. Keep the old engine folder until the end:

```bash
mv ~/ClaudeBot ~/ClaudeBot.old
```

4. Clone the repository and move to the release (use your repository's
address):

```bash
git clone <repository-url> ~/ClaudeBot
```

```bash
cd ~/ClaudeBot && git checkout v0.0.2
```

5. Install over the existing company home. This updates the database, the
`cb` links and the unit files, and starts the services:

```bash
engine/install.sh --start "My Company" "$CLAUDEBOT_HOME"
```

6. Refresh the rules and skills, and relink them (the old links point into
the folder you moved):

```bash
cb templates refresh
```

```bash
cb bot relink
```

7. If the always-on Admin is enabled:

```bash
sudo systemctl restart claudebot-admin
```

8. Check, and remove the old folder once `cb doctor` is clean:

```bash
cb doctor
```

```bash
rm -rf ~/ClaudeBot.old
```

To go back before step 8: stop the services, move `~/ClaudeBot.old` back into
place, restore `pre-clone-move.db` as described under "If an update fails",
and start the services.

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
git checkout v0.0.1   # the version you were on
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

An update replaces the engine code and the engine-owned files in the company
home. It leaves the company's own files alone:

- Each bot's Soul and persona are not rewritten. A persona is changed only
  with the Owner's agreement; a Soul only by the Owner. For existing installs,
  the Owner should manually update `$CLAUDEBOT_HOME/bots/backend/SOUL.md` under
  "What I will never do" to add:
  - Stop or restart the service I run inside. A change to the live install
    runs on its own (`cb update`) and ends with a restart, never a stop.
  - Restart a service from a health check: the health check alerts, it never
    restarts a service.
- Memory files, company work, secrets and `config.json` are not touched.

Engine-owned, and refreshed by `cb templates refresh` on every update from
0.0.2 on:

- `rules.md` in the company home, when it still matches a template this
  project shipped. If you have edited it, the update leaves your file alone
  and writes the new template as `rules.md.new`. Compare the two and keep
  the company's rules. `cb doctor` reports `rules-new` until `rules.md.new`
  is removed. The company home is a Git repository, so
  `git -C "$CLAUDEBOT_HOME" log -p rules.md` shows earlier versions.
- The bundled skills in `skills/` in the company home (`cb-basics`,
  `work-tracking`, `create-bot`). Skills you added under other names stay.

When a release changes a bot's starting persona, the CHANGELOG says so, and
the new version is in the engine's `engine/templates/` folder.

## Verifying releases

Maintainers sign a release with `git tag -s`. To make this clone refuse an
unsigned or wrongly signed tag, set the fingerprint `git verify-tag` prints:

```bash
git config claudebot.releaseKey <fingerprint>
```

`engine/update.sh` then runs `git verify-tag` and continues only when that
fingerprint is in the output. With no key configured it prints one note and
continues. The check happens before the checkout or the database backup.
