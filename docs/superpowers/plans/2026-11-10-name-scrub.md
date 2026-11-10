# Name scrub: company, product, and server identifiers out of the engine

**Date:** 2026-11-10
**Status:** Scrub implemented on chore/name-scrub / PR #27.
**Scope:** The engine repo's tracked files. Git history rewriting is a separate decision (see Owner decisions).

## Intent

The engine is reusable and ships to customers, so its docs and tests must be company-agnostic. Company-specific names, server addresses, and ops details belong in an Owner-only note outside the repo (per `CLAUDE.md`). This plan removes them from tracked files on a new branch, without rewriting history.

## Placeholders

Every term below is replaced with a generic placeholder. The Owner keeps the real values in a private note.

| Category | Placeholder |
|---|---|
| Company name | `<COMPANY>` |
| Product line name | `<PRODUCT>` |
| Owner's first name | `the Owner` |
| Company home path | `$CLAUDEBOT_HOME` or `/home/claude/<COMPANY>` |
| SSH alias for the VPS | `<VPS_ALIAS>` |
| GitHub org for backups | `<ORG>` |
| Telegram bot username | `<BOT>` |
| Hosting provider name | `<provider>` |
| Old server aliases | `<old-alias>` |

## Files and actions

Line numbers refer to the checked-out branch `chore/land-review-fixes-stack`.

| File | Hits | Action |
|---|---|---|
| `README.md` 145–148 | 3 | Replace the company block with placeholders. Delete the "older docs" sentence, which only exists because of the rename. |
| `docs/deploy.md` 19–65 | 8 | Replace the SSH alias and paths with placeholders. Keep the generic deploy steps. |
| `docs/ops-checklist-review-backlog.md` | 11 | **Move out of the repo** to the Owner-only note. It is an ops checklist: GitHub org settings, deploy keys, and SSH aliases. |
| `docs/superpowers/plans/acceptance-notes.md` | 27 | **Move out of the repo.** It holds a server hostname, deploy-key file names, a bot username, and per-run transcripts. |
| `docs/superpowers/plans/preflight-notes.md` | 1 | Replace the server directory with a placeholder. Keep the notes, since they are generic. |
| `docs/superpowers/plans/2026-11-08-claudebot-engine.md` | 4 | Replace names with placeholders. This is a historical plan, so keep its structure. |
| `docs/superpowers/plans/2026-11-09-claudebot-cost-control.md` | 1 | Replace the alias with `<VPS_ALIAS>`. |
| `docs/superpowers/specs/2026-11-08-claudebot-engine-design.md` | 11 | Replace the company and product names. Remove the Owner's first name, using "the Owner" instead. Keep the design rules. |
| `tests/test_archive.py` 45 | 1 | Change the project directory to `-home-claude-company-bots-backend`. |
| `tests/test_templates.py` 149 | 1 | See decision 2 below. |

Files not on the list had no hits on this branch. `CHANGELOG.md`, `UPDATING.md`, and `engine/install.sh` only matched a broader pattern and are generic.

## Steps

1. **Branch.** Create `chore/name-scrub` from `chore/land-review-fixes-stack`, not `main`. The scrub depends on that stack's docs.
2. **Move the Owner-only notes.** Copy `ops-checklist-review-backlog.md` and `acceptance-notes.md` to the Owner-only location chosen in decision 1. Then delete them from the repo in the same commit.
3. **Replace names.** Apply the placeholder table to the remaining files. Use one commit per category (docs, specs and plans, tests) so review is easy.
4. **Guard.** Run `git grep -n -i -E '<terms>'` with the Owner's private term list. Expect no hits outside `tests/test_templates.py`.
5. **Verify.** Run the unit suite: `python3 -m unittest discover -s tests`. Confirm `test_templates` and `test_archive` pass.
6. **Push the branch and open a PR.** Do not merge it until the Owner has reviewed the diff.

## Owner decisions

1. **Where do the moved notes go?** Options: a private notes repo, or a folder outside the engine history (for example `~/<COMPANY>-ops`). Recommended: a private notes repo, so they are backed up and searchable without touching the engine.
2. **`test_templates.py` banned-term list.** Option A: keep the real names in the test. Fast, but the names ship to customers. Option B: read the banned terms from an environment variable, `CB_BANNED_TERMS`, and skip the check when it is unset. Recommended: B, with the check running in your own CI through that variable.
3. **Git history.** Removing the IP and keys from old commits requires a force-push to `origin`, re-cloning on every machine, and a fresh decision on `backup/pre-split`. This plan does not do that. Decide separately.
4. **Delete the local `refs/backup/pre-split-1794338612`?** It holds a full copy of the names and is local only. Recommended: delete it once the scrub PR is merged, since the reflog keeps it recoverable for a while.
5. **Release.** The `v0.0.1` tag is already pushed and its tree carries the names. Moving or re-tagging it would break anyone who has it. Recommended: leave `v0.0.1` alone and ship the scrub as `v0.0.2`.

## Out of scope

- Failing CI on `main` (6 failures, 16 errors in the 413-test run). It blocks any release and needs its own plan.
- Confirming who holds the `owner@example.com` key on the VPS. That needs an SSH check, which requires Owner approval.
- Access to the `<ORG>` backup repos, which this account cannot see.
- The `.gitignore` gaps for `company.db`, `.secrets/`, logs, and transcripts. Small, separate PR.

## Done when

- The grep in step 4 returns no hits outside the Owner's private list and the guard test.
- The unit suite passes on the branch.
- The moved notes exist in the Owner-only location, and the repo has no copy.
- The Owner has approved decisions 1, 2, 4, and 5.
