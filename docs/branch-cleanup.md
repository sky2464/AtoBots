# Branch cleanup

Housekeeping for the engine repository on GitHub. Do not force-push `main`.

## Current state (2026-11-10)

The GitHub remote **`sky2464/claudebot`** exposes only **`main`** as a branch.
Merged review and feature branches have been removed (or were auto-deleted on merge).

Your local clone may still list stale remote-tracking branches until pruned:

```bash
git fetch --prune origin
git branch -r
```

After `fetch --prune`, expect `origin/main` only (plus `origin/HEAD`).

## Keep it that way

- Enable **Automatically delete head branches** (Settings → General → Pull Requests).
- Delete local branches that tracked removed remotes:
  `git branch -d <branch>` when `git branch -vv` shows a gone upstream.
- Do not recreate long-lived topic branches on the remote unless a PR needs them.

## Historical note

Early work used branches such as `plan/claudebot-engine` and `docs/readme-polish`.
That content lives on `main` under `docs/superpowers/`; those branch names are
obsolete on GitHub.
