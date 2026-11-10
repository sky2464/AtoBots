# Cutting and shipping a release

For maintainers. Servers are updated with `engine/update.sh`; the steps an
operator follows are in [UPDATING.md](../UPDATING.md).

## Cut a release

1. On a branch: set `__version__` in `engine/claudebot/__init__.py`, add the
   release's section to `CHANGELOG.md` (with "Manual steps after updating" when
   there are any), and update version examples in `README.md` and
   `UPDATING.md`.
2. Merge to `main` through a pull request with CI green.
3. Tag the merge commit and push the tag. The tag must be `v` plus the package
   version, because `engine/update.sh` picks the newest `v*` tag:

```bash
git switch main && git pull --ff-only
```

```bash
git tag -a v0.0.2 -m "ClaudeBot v0.0.2"
```

```bash
git push origin v0.0.2
```

4. Publish a GitHub Release for the tag with the CHANGELOG section as its
   notes.

Never move or re-create a published tag. Fix forward with a new version.

## Ship it to a server

On the server, in the clone, as the account that runs the bots:

```bash
engine/update.sh
```

Then do the release's "Manual steps after updating", if it has any.

The server must hold a full Git clone. Do not rsync engine files into it: that
leaves removed modules behind or deletes `.git`, and `engine/update.sh` then
refuses to run. A server that was set up by copying files is moved to a clone
once; see "Moving a copied install to a clone" in
[UPDATING.md](../UPDATING.md).

Company data lives under `CLAUDEBOT_HOME`, never inside the clone.
