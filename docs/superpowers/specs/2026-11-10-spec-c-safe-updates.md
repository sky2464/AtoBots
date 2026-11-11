# Spec C — A safe update path

Fixes findings L3, L4, L8, L9, L12, L15, S8, S12 in
`docs/review/2026-11-10-review-findings.md`. Target release: 0.0.3.

## Goal

`engine/update.sh` should leave a running company in a known state: no run
killed half-way, every unit file current, the Owner's `rules.md` kept, and a
clear answer to "can the older engine open this database?".

## Rules for the builder

- Bash for the scripts (`set -euo pipefail`, pass `shellcheck`), Python
  standard library for the engine. One task = one commit, in order.
- Python changes come with unit tests in the named file. Shell changes are
  covered by the text checks in `tests/test_templates.py` (it already reads
  the scripts as text); add assertions there. No manual or server testing is
  part of this spec.
- Never add a step that touches SSH, the firewall or sudo rules.
- Do not write any company, product, person or server name anywhere.

## Task C1 — Schema version (L9)

Files: `engine/atobots/db.py`, `engine/atobots/cli.py`. Tests:
`tests/test_db.py`.

1. `SCHEMA_VERSION = 2` in `db.py`. (Version 1 is every database made by
   0.0.1 or 0.0.2.)
2. `_migrate` reads `meta.schema_version` (missing = 1), runs the existing
   column probes only when the stored version is lower than `SCHEMA_VERSION`,
   then writes the new value. On a current database it does one `SELECT` and
   returns.
3. If the stored version is **higher** than `SCHEMA_VERSION`, `connect()`
   raises `RuntimeError("this company database was written by a newer
   AtoBots (schema N); update the engine or restore the pre-update
   backup")`. `cli.main` already prints `RuntimeError` and returns 1.
4. `cb version --schema` prints the engine's schema version and, when a
   company home is configured, the database's.

Done when: tests cover a version-1 database being upgraded, a current one
being left alone (assert no `ALTER`, by counting statements through
`conn.set_trace_callback`), and a newer one being refused.

## Task C2 — Pause the dispatcher during an update (L3)

Files: `engine/atobots/dispatcher.py`, `engine/update.sh`. Tests:
`tests/test_dispatcher.py`, `tests/test_templates.py`.

1. New commands `cb dispatcher hold --minutes N --reason TEXT` and
   `cb dispatcher release`. `hold` writes `meta` key
   `dispatcher.hold_until` (UTC ISO) and `dispatcher.hold_reason`; `release`
   deletes both. `Dispatcher._paused` treats an unexpired hold exactly like
   the usage-limit pause (no new runs; steps before it still run).
   `status_text` shows `Held until: … (reason)`.
2. `update.sh` order becomes:
   1. checks, fetch tags, pick tag, database backup (unchanged);
   2. `cb dispatcher hold --minutes 45 --reason update` (old code);
   3. wait until `cb dispatcher idle`, up to 30 minutes;
   4. if still busy: print the running bots (`cb status`) and **stop with
      exit 1 after `cb dispatcher release`**, unless `--force` was given;
   5. `sudo systemctl stop atobots-dispatcher`;
   6. checkout the tag; `CHANGED=1`;
   7. `cb init`, `cb templates refresh` (see C3);
   8. re-render units (see C4), `daemon-reload`, start services;
   9. `cb dispatcher release`; `cb doctor`.
3. `on_error` also runs `cb dispatcher release` (ignore its failure) so a
   failed update never leaves the queue held.
4. An engine older than this release has no `hold` command: when
   `cb dispatcher hold` exits 2, print "this version cannot hold the queue;
   continuing as before" and go on.

## Task C3 — Keep the Owner's `rules.md` (L4)

Files: `engine/atobots/cli.py`, `UPDATING.md`. Tests:
`tests/test_templates.py`.

`refresh_engine_templates` changes:

1. Record the SHA-256 of the template it installs in `meta` key
   `templates.rules_sha`.
2. On refresh, compare the company's `rules.md` to that recorded hash:
   - equal (the Owner never edited it): replace it, update the hash;
   - different: leave it alone, write the new template next to it as
     `rules.md.new`, and print
     `rules.md has local changes; the new version is in rules.md.new`.
   - no recorded hash (installs from 0.0.2 or earlier): compare against the
     hashes of the templates shipped in 0.0.1 and 0.0.2 (put both hex strings
     in a tuple `KNOWN_RULES_SHA` in `cli.py`); a match counts as unedited.
3. Bundled skills keep being replaced (they are engine-owned), but through a
   temp folder and `os.replace`, so a crash cannot leave a skill half-copied.
4. `cb doctor` reports a finding `rules-new` while `rules.md.new` exists.

`UPDATING.md`: replace the two conflicting sentences with this behaviour.

## Task C4 — Render every unit in one place (L3, L12)

Files: `engine/install.sh`, `engine/update.sh`, new `engine/lib/units.sh`,
`engine/systemd/atobots-admin.service`. Tests: `tests/test_templates.py`.

1. Move `render_unit` into `engine/lib/units.sh` with one function
   `install_units HOME_DIR ENGINE_DIR RUN_USER MEMORY_MAX` that renders the
   dispatcher, backup, doctor (services and timers) **and** the Admin unit.
   Both scripts source it. Delete the duplicated `sed` blocks in `update.sh`.
2. Add `@MEMORY_MAX@` to the Admin unit in place of the fixed `1200M`; the
   scripts pass `cb config get run_memory_max` (add that tiny read-only
   command: prints one config value; unknown key exits 1).
3. Remove the "2 GB host" comment from the Admin unit.
4. `update.sh` restarts the Admin unit only when its rendered text changed or
   the engine version changed, and prints that the conversation will restart.

## Task C5 — `cb hook stop` is a no-op again (L8)

File: `engine/atobots/requests.py`. Tests: `tests/test_requests.py`.

Register `hook stop` with a handler that reads and discards stdin and returns
0. Help text: "kept for sessions started by an older engine; does nothing".

## Task C6 — Prune old pre-update copies (L15)

File: `engine/update.sh`. After a successful update keep the five newest
`pre-update-*.db` files in `$ATOBOTS_HOME/backups` and delete older ones;
print each deleted name.

## Task C7 — Verify the release tag when a key is configured (S8)

Files: `engine/update.sh`, `UPDATING.md`.

If `git config --get atobots.releaseKey` (in the clone) returns a
fingerprint, run `git verify-tag "$TAG"` and require that fingerprint in its
output; fail before any change otherwise. With no key configured, print one
line: `note: release tags are not verified (see UPDATING.md, "Verifying
releases")`. Document how to set the key and that maintainers sign with
`git tag -s`.

## Task C8 — `/usr/local/bin/cb` wrapper (S12)

File: `engine/install.sh`. Instead of a symlink into the clone, install a
two-line root-owned wrapper that refuses to run as uid 0:

```sh
#!/bin/sh
[ "$(id -u)" -ne 0 ] || { echo "cb: do not run as root" >&2; exit 1; }
exec @ENGINE@/bin/cb "$@"
```

## Out of scope

- Narrowing passwordless sudo (finding S1).
- Converting rsync installs: documented in `UPDATING.md`, not automated.
