# Making a release

A Git tag alone does not put anything on the repository's **Releases** page.
The `Release` workflow creates the GitHub Release when a version tag is pushed.

1. In a pull request, bump `__version__` in `engine/atobots/__init__.py` and
   add a `## X.Y.Z - date` section to `CHANGELOG.md` (what changed, and any
   manual steps after updating). The section becomes the Release notes, and
   the workflow refuses to run without one. Merge it once CI is green.
2. Tag the merge commit on `main` and push the tag. Maintainers sign it
   (`git tag -s`, see [UPDATING.md](UPDATING.md)):

   ```bash
   git switch main && git pull --ff-only
   git tag -s vX.Y.Z -m "AtoBots X.Y.Z"
   git push origin vX.Y.Z
   ```

3. Watch the `Release` run in the Actions tab, then check the Releases page.

The workflow reads the notes from `CHANGELOG.md` on `main` and only accepts a
tag whose commit is part of `main`'s history. To publish a Release for a tag
that already exists, run the `Release` workflow by hand and give it the tag
(Actions, Release, Run workflow); it is safe to run again.

`engine/update.sh` installs from the newest `v*` tag, so a version is
available to installs only once its tag is pushed.
