# Spec D — An engine-only repository

Fixes findings S9, S10, S11, D1, D2, D3, D6, D7, L11, F1–F10 in
`docs/review/2026-11-10-review-findings.md`. Target release: 0.0.3, except
Task D7, which the Owner runs by hand.

## Goal

The repository should hold a reusable engine and nothing about one company,
one person or one server — in the tree, in the tags and in the history a
customer receives when cloning.

## Rules for the builder

- Tasks D1–D6 are ordinary commits, one per task, in order.
- **Do not run Task D7.** It rewrites history and deletes tags; it is a
  checklist for the Owner. Do not force-push anything.
- Never write the private names, the server address or key material into any
  file, test, commit message or PR text — including to "search for" them. The
  list of banned words is supplied at run time through `CB_BANNED_TERMS`.
- Run `python3 -m unittest discover -s tests` before each commit. No manual
  or server testing is part of this spec.

## Task D1 — Remove files that do not belong (F1, F2, F4, F5, F9, D6)

Delete:

- `docs/branch-cleanup.md`
- `docs/deploy.md`
- `docs/hygiene-leftovers.md`
- `docs/superpowers/plans/` (the whole folder, including `preflight-notes.md`)
- `tests/__init__.py`

Before deleting `hygiene-leftovers.md`, open one GitHub issue per heading in
it (title = the heading, body = the section text).

Edit `README.md`:

- remove the section "First company install (ops)";
- "Deploy" becomes two sentences pointing at `UPDATING.md`;
- "Documentation" lists the two design specs and `UPDATING.md` only;
- "Repository layout": add `update.sh`, remove `vps-bootstrap.sh` (Task D2);
- "Command overview": add `usage`, `task` (`send` / `finish`), `message`
  (`done` / `retry`), `templates refresh`, `log sudo`; `check` moves under
  `bot` (`bot check`, `bot relink`).

Edit `CLAUDE.md`: remove the pointer to acceptance notes.

Add a test in `tests/test_templates.py` that every relative Markdown link in
`README.md`, `UPDATING.md` and `CHANGELOG.md` points at a file that exists.

## Task D2 — Move server hardening out of the engine (D1)

1. Delete `vps-bootstrap.sh`.
2. `CLAUDE.md`: remove the "Bootstrap" section and the words "on a private
   VPS"; keep the four rules, reworded for any server.
3. `README.md` "Requirements" gains: "A server account for the bots with
   passwordless sudo (the engine starts each run with `sudo systemd-run`).
   Securing the server itself — SSH, firewall, updates — is your job and is
   outside this project."
4. `docs/templates/server-inventory.md` stays (it is a blank template).

The script itself is not lost: it stays in Git history until D7, and the
Owner keeps a copy in a private ops repository before this task is merged.
State that in the PR description.

## Task D3 — Neutral templates and prompts (D2, L11)

Files: `engine/templates/rules.md`, `engine/templates/admin/PERSONA.md`,
`engine/templates/backend/PERSONA.md`,
`engine/templates/skills/*/SKILL.md`, `engine/claudebot/admin_live.py`
(`LIVE_PROMPT`), `engine/claudebot/chat.py` (`_compose_prompt`),
`engine/claudebot/rotation.py` (`build_summary`),
`engine/claudebot/requests.py`, `engine/claudebot/routines.py`,
`engine/claudebot/db.py`, `engine/claudebot/config.py`.

1. The Owner is "the Owner" / "they" everywhere; no "he", "him", "his".
2. `rules.md` "Other outside actions": replace the email / video wording with
   "Sending messages, posting, editing or deleting published content, signups
   and commitments to other people are prepared by bots and need the Owner's
   direct word to Admin, recorded the same way." and "Admin may read connected
   services on its own, read-only, including on routines, once they are set
   up." Same change for the matching Persona line.
3. Time zone: default `timezone` becomes `"UTC"`. `db._zone_label` returns
   `d.tzname()` always (drop the special `CT` case). Replace every "Central
   Time" / "CT" in templates and help text with "the company's time zone".
   `cb init` gains `--timezone ZONE`; when given it writes `timezone` into
   `config.json` (create the file if missing, keep other keys).
   `install.sh` passes `--timezone "$(cat /etc/timezone 2>/dev/null || echo
   UTC)"` on first setup only.
   **Upgrade rule:** in `Config._apply_overrides`, when `config.json` has no
   `timezone` key and the database already holds routines or decisions, keep
   `America/Chicago` and have `cb doctor` report finding `timezone-unset` that
   says to add the key. This keeps existing schedules where they are.
4. `config.py`: `claude_projects_dir` defaults to
   `Path(self.run_home) / ".claude" / "projects"`.
5. `cli.py`: `ADMIN_MODEL` and `BACKEND_MODEL` are removed; `init_company`
   uses `cfg.default_model`.
6. `admin_live.TOKEN_SECRET` stays, but `install.sh` asks the engine instead
   of grepping: add `cb admin has-token` (exit 0 / 1).

Update the template tests to the new wording. Add a test that no file under
`engine/templates/` contains the whole words `he`, `him`, `his` or `Central`.

## Task D4 — A name guard that really runs (S10)

Files: `tests/test_templates.py`, `.github/workflows/ci.yml`.

1. The scan covers every tracked file (`git ls-files`), not only `engine/`,
   and is case-insensitive.
2. New test: tracked text files contain no IPv4 address other than
   `127.0.0.1` and `0.0.0.0`, and no line matching
   `ssh-(ed25519|rsa) AAAA`.
3. CI sets `CB_BANNED_TERMS: ${{ secrets.CB_BANNED_TERMS }}`. When the secret
   is empty the test **fails** on the `main` branch and skips elsewhere (read
   `GITHUB_REF_NAME`). The Owner adds the secret in the repository settings;
   say so in the PR description without listing the words.
4. A second CI step scans the commit subjects and bodies in the push range for
   the same words.

## Task D5 — CI and repository settings (S11)

`.github/workflows/ci.yml`:

- `on: push: branches: [main]` and `pull_request:` (no double runs);
- top-level `permissions: contents: read`;
- pin `actions/checkout` and `actions/setup-python` to full commit SHAs with
  the version in a comment;
- add a `shellcheck engine/*.sh engine/lib/*.sh` step.

Add a `LICENSE` file only after the Owner names the licence (ask in the PR;
do not pick one).

In the PR description, list the settings only the Owner can change: turn on
"Automatically delete head branches", add a ruleset for `main` (pull request
required, status check `test` required) if the plan allows it, turn on secret
scanning.

## Task D6 — Local leftovers (F3, F6, F7, F8) — instructions only

No commit. Put this checklist in the PR description for the Owner to run on
their own machine:

- move `backups/*.tar.gz` out of the checkout (they hold company data);
- delete the untracked `AGENTS.md` and the untracked name-scrub plan, or
  commit a corrected `AGENTS.md` (its `NEW_USER=` example is wrong);
- `git branch -vv | grep ': gone]'` lists the 22 branches whose remote is
  deleted; delete them after checking `git log main..BRANCH` is empty or
  already merged through a PR;
- `integrate/review-fixes` and `feat/cost-control-v0.0.1` are local only:
  compare with `git diff main...BRANCH --stat` before deleting;
- `git update-ref -d refs/backup/pre-split-<number>` once nothing is needed
  from it.

## Task D7 — Clean history before any customer clones (S9) — Owner only

Not for a build agent. The choice and the commands are the Owner's.

Recommended: **publish customers a fresh repository** rather than rewriting
this one.

1. Finish D1–D5 and tag the result.
2. Create a new empty repository. Export the tagged tree with
   `git archive` into it and make one commit ("ClaudeBot <version>") with a
   project identity (for example `ClaudeBot <noreply@…>`), then tag it.
3. Customers clone only the new repository. `engine/update.sh` works there
   unchanged, because it only needs `origin` and `v*` tags.
4. Keep this repository private as the development history, or archive it.

If the Owner prefers to rewrite this repository instead: use
`git filter-repo --replace-text` and `--replace-message` with a private
word list, re-create the tags, force-push with every clone and server
re-cloned afterwards, edit the text of PR #2 and #3 by hand, and ask GitHub
Support to purge cached views. The engine's "never force-push" rule applies to
the *backup and archive* remotes, not to this one, but the step still needs
the Owner's explicit go-ahead.

Either way: treat the server address and the two public keys in the old
history as known. Rotating the keys and reviewing who had read access is the
Owner's call.
