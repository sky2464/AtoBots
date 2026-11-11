# GitHub Releases for every version tag — design

Status: building now (small change, written down so the next release follows
the same path).

## Why

The repository's **Releases** page is empty even though version tags exist.
A Git tag and a GitHub Release are two different things: pushing `v0.1.0`
creates only the tag. Nobody ever created the Release objects, so the page
shows "There aren't any releases here". Two other gaps surfaced while looking:

- `v0.0.2` and `v0.2.1` have changelog sections (and merged "release" pull
  requests) but were never tagged. `engine/update.sh` picks the newest `v*`
  tag, so an install asking for "the newest release" would be offered
  `v0.1.0`, not `0.2.1`.
- Releases have no repeatable recipe, so each one depends on someone
  remembering the steps.

## Goals

- Pushing a tag `vX.Y.Z` creates a GitHub Release for it automatically, with
  the matching `CHANGELOG.md` section as its notes.
- The existing versions get Releases too, and the missing tags are created.
- The recipe for making a release is written down in one short place.

## Non-goals

- Building or attaching binaries, zips or packages. Installs come from a Git
  tag, as today.
- Signing tags (see UPDATING.md, "Verifying releases"); unchanged.
- Changing how `update.sh` picks or verifies tags.

## Design

- A workflow `.github/workflows/release.yml` runs on a pushed tag that looks
  like `vX.Y.Z`, and by hand (`workflow_dispatch`, with the tag as input) to
  create the Release for a tag that already exists.
- It checks out the default branch, never the tag, and reads
  `CHANGELOG.md` from there. This keeps notes current and means older tags
  (made before the rename) never publish their old text.
- Safety checks, in order, each failing the run before anything is created:
  the tag matches `vX.Y.Z`; the tag exists; the tag's commit is part of the
  default branch's history; `CHANGELOG.md` has a non-empty `## X.Y.Z` section.
- A small stdlib script, `.github/scripts/release_notes.py`, extracts the
  section; it has unit tests.
- The release is created with `gh release create --verify-tag`, or edited if
  it already exists, so re-running is safe. A manual run for an old tag does
  not mark it "latest".
- Permissions: the workflow alone gets `contents: write`; `ci.yml` stays
  read-only. Actions are pinned to the same commit SHAs as `ci.yml`.
- `docs/releasing.md` lists the steps: changelog section and version bump
  merged first, tag the merge commit, push the tag, check the Releases page.

## My calls

- Release title is "AtoBots X.Y.Z"; releases are not marked pre-release.
- Notes come from the default branch's changelog, not the tag's.
- `v0.0.2` goes on the existing "release: 0.0.2" commit (the one that set the
  version to 0.0.2); `v0.2.1` goes on the merge commit of the pull request
  that released it.

## Done when

- Tests pass, including new ones for the notes script and the workflow's
  shape.
- The Releases page lists 0.0.1, 0.0.2, 0.0.3, 0.1.0 and 0.2.1, with 0.2.1
  marked latest.
