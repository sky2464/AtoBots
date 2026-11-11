# Migrations

A release can ship a script here that moves an existing install forward, so
nobody has to follow a manual recipe. The update's second half
(`claudebot/update_finish.py`) runs them after `cb init` and before the units
are installed.

## Naming and order

`<version>-<short-name>.sh`, for example `0.1.0-rename.sh`. During an update
from OLD to NEW, every script with `OLD < version <= NEW` runs, in version
order. A `+local.<hash>` suffix on a version is ignored when ordering. Each
script that succeeds is recorded in the company database
(`meta.migrations.applied`).

## Rules for a script

- Bash, starting with `set -euo pipefail`, and it passes `shellcheck`.
- Safe to run twice. Check before changing; never assume a clean start.
- No SSH, firewall, sudo rules or secrets. Never delete company data.
- Leave the OLD version able to run. If the update fails later, the code,
  the database and the unit files are rolled back, but nothing else a
  migration did is undone. So add, do not remove: write the new file and
  keep the old one for a release.
- Do not start, stop, enable or rename the engine's own units: the update
  does that, and its rollback depends on knowing what was there.
- Finish within 15 minutes; a script that takes longer is stopped and the
  update is rolled back.
- Output goes to the update log. Print what was done in plain words.

## What a script is given

Environment variables: `CB_ENGINE_DIR` (the new engine folder),
`CB_HOME_DIR` (the company home), `CB_OLD_VERSION`, `CB_NEW_VERSION`,
`CB_OLD_PREFIX` (unit-name prefix before the update). Source
`"$CB_ENGINE_DIR/lib/sysfiles.sh"` for `cb_priv` and `install_root_file`
(which never writes through a symlink), and `lib/names.sh` for the current
names.

A non-zero exit fails the update and starts the rollback.
