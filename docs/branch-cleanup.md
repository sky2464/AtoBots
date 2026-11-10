# Stale branch cleanup

Safe housekeeping for the `sky2464/claudebot` remotes after the review stack.
Do this on the Mac checkout; do not force-push `main`.

## Known stale remotes (as of 2026-11-09)

| Ref | Why it is stale | Suggested action |
|---|---|---|
| `origin/plan/claudebot-engine` | Early planning branch; work lives in `docs/superpowers/` on `main` | Delete remote + local after confirming no open PR |
| `origin/docs/readme-polish` | Already merged to `main` (PR #1) | Delete remote branch if GitHub did not auto-delete |
| Local `plan/claudebot-engine` | Tracks the stale remote | `git branch -d plan/claudebot-engine` after remote delete |

## Commands

```bash
git fetch --prune origin
git branch -r

# After Owner confirms nothing still needs the planning branch:
git push origin --delete plan/claudebot-engine
git branch -d plan/claudebot-engine   # if the local branch exists

# Merged polish branch (if still present):
git push origin --delete docs/readme-polish
```

Enable **delete branch on merge** in the GitHub repo settings so future PR heads
do not linger. Review-stack branches (`fix/review-pr*`) should be removed the
same way once each PR merges.
