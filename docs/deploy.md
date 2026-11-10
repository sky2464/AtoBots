# Deploy ClaudeBot from a tagged checkout

Ship a known engine revision to the VPS. Prefer a Git tag over an arbitrary
branch tip so the server matches a reviewable commit.

Live acceptance evidence is recorded in
[acceptance-notes.md](superpowers/plans/acceptance-notes.md).

## Version

- Package / CLI version string: `claudebot.__version__` (printed by `cb version`).
- Release tags look like `v0.0.1` on `main`. Create annotated tags on `main` after
  the commit you intend to ship; do not retag rewritten history.

```bash
git tag -a v0.0.1 -m "ClaudeBot v0.0.1"
git push origin v0.0.1
```

## Server layout

The VPS should keep a **full Git clone** of this repository (including `.git`),
not a copied tree from rsync. `engine/update.sh` uses `git fetch`, `git checkout`,
and rollback via `git checkout <old ref>`; rsync-only trees break updates and
can delete `.git` if `--delete` is used against the checkout root.

- **Engine checkout:** e.g. `~/ClaudeBot` (clone of the engine repo).
- **Company home:** `CLAUDEBOT_HOME` (set by `engine/install.sh`), holding
  `company.db`, bots, work, and `.secrets/`. Deploy and update **never** replace
  the company home.

Owner-specific hostnames, SSH aliases, and inventory live in
[server-inventory.md](templates/server-inventory.md) (copy outside the repo; do
not commit real values).

## First install (new server)

As user `claude` on the VPS:

```bash
git clone git@github.com:ORG/claudebot.git ~/ClaudeBot
cd ~/ClaudeBot
python3 -m unittest discover -s tests
engine/install.sh --start "My Company" /home/claude/MyCompany
```

Use your real org/repo URL and company name. See [UPDATING.md](../UPDATING.md) for
day-to-day upgrades.

## Update an existing install

On a machine that can push tags (typically your laptop):

```bash
git fetch --tags origin
git tag -a v0.0.2 -m "ClaudeBot v0.0.2"   # when ready
git push origin v0.0.2
```

On the VPS, in the existing clone:

```bash
cd ~/ClaudeBot
git fetch --tags origin
engine/update.sh                 # newest v* tag
# or: engine/update.sh --tag v0.0.1
```

`engine/update.sh` backs up the company database, checks out the tag, runs
`cb init`, restarts services, and runs `cb doctor`. See [UPDATING.md](../UPDATING.md)
for rollback steps.

Optional sanity check after update:

```bash
python3 -m unittest discover -s tests
cb version
cb doctor
```

## What not to do

- Do **not** `rsync -a --delete` into `~/ClaudeBot` on the server. That can
  remove `.git` and leaves no supported update path. If you must copy files
  once for bootstrap, clone from Git instead.
- Do **not** rsync or overwrite the company home when shipping engine changes.
- Do **not** create or push a GitHub Release unless the Owner asked; tagging
  `main` is enough for `engine/update.sh`.

## When systemd units change

`engine/update.sh` does not reinstall unit files. If a release changes files
under `engine/systemd/`, read that release in [CHANGELOG.md](../CHANGELOG.md)
and re-run `engine/install.sh` (no `--start` required) or install units by hand
as documented there.
