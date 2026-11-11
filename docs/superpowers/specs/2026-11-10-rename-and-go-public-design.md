# Rename and go public — design

Status: **Part A (the name) BUILT in 0.1.0**: the project is now AtoBots,
and the updater moves existing installs. **Part B (going public) is decided
but not done**: see [the readiness decision](../../public-readiness.md); four
items wait for the Owner. What follows is the design as it was written before
the build, under the old name.

## Why

The Owner wants to publish the engine. Two things stand in the way:

1. **The name.** The project is called "ClaudeBot". "Claude" is Anthropic's
   trademark, and the Owner's reading is that Anthropic does not want it used
   in other people's product names. That is a sound instinct for a public
   project. I cannot give legal advice; the first task is to read Anthropic's
   published brand and usage guidance and pick a name that clearly stays
   outside it. (Saying that the engine "works with Claude Code" is a different
   matter from putting the word in the product's own name; the guidance
   decides what wording is allowed.)
2. **Whether the repository is fit to be public.** Earlier reviews found that
   one server's address, SSH key paths and company names reached the Git
   history and a release tag, and that the engine's bots all run under one
   account with passwordless sudo. Those findings were not re-checked while
   writing this; the first step is to verify what is still true.

## Goals

- A new name, chosen with checks, applied everywhere the old one appears,
  with existing installs moved over by the updater and no lost data.
- A written readiness decision: ready, or ready after the listed fixes.
- If ready: a public repository whose tree, tags and history contain nothing
  about one company, person or server.

## Non-goals

- Marketing, a website, a logo or community building.
- A support promise or a hosted service.
- Changing what the engine does.

## Anti-goals

- A rename that leaves live installs half-converted (old unit names running
  next to new ones, secrets not found, the Admin chat gone).
- Going public with a history that still holds the server address, key paths
  or customer-identifying words, even if the current tree is clean.
- Publishing under a name that only moves the trademark problem somewhere else.

## Part A — The name

1. Shortlist and check: Anthropic's brand guidance, GitHub organisation and
   repository availability, package-name and domain availability, and a basic
   trademark search in the Owner's market. A lawyer's view is the Owner's call.
2. Decide what stays. The short command `cb` is probably fine to keep (it is
   not the name), but that is an open question.
3. Inventory what carries the old name (to be confirmed by search at the
   time): the Python package folder, the install folder, the environment
   variable and file (`CLAUDEBOT_HOME`, `/etc/claudebot.env`), systemd unit
   and timer names, the sudoers entries, the Telegram channel and secret
   names, config and database keys, log and backup file names, tests, docs,
   the README, the licence header, and the repository itself.
4. Migration for existing installs, as a migration script in
   `engine/migrations/`: stop services, rename folders and units, keep the old
   environment variable readable for one release (with a doctor warning),
   re-render units, relink skills, restart, verify. It must be safe to repeat
   and must leave the company home and secrets where they are.
5. Release the rename as its own version with a clear changelog and a
   rollback that works.

## Part B — Readiness to be public

Decide each item with the Owner and record it:

- **History.** Either publish a fresh repository from a clean export of a
  tagged tree (recommended in an earlier review, because nothing needs
  rewriting) or rewrite this one. Rotating the keys and reviewing who had
  read access to the old history are the Owner's calls.
- **Licence.** Not yet chosen. The Owner decides; the builder must not pick.
- **Security posture.** Every bot runs under one account with broad sudo. A
  public README and a `SECURITY.md` must say so plainly and say how to report
  a problem. Narrowing the sudo rules was left as an Owner decision in an
  earlier review; decide whether it blocks publishing.
- **Dependencies on Anthropic's products.** The always-on Admin uses Claude
  Code and its Telegram channel plugin. State the requirements and versions,
  and check that the plugin's terms allow this use.
- **CI and settings.** Tests green on `main`, name and secret scanning in CI,
  branch protection, secret scanning on. The earlier "engine-only repository"
  spec (Spec D) lists most of this; check how much of it has been done.
- **Docs for strangers.** A README that works for someone with no history
  here: requirements, install, update, uninstall, a first company, what it
  costs to run, what it will not do.
- **Support boundary.** What the Owner will and will not answer.

## Open questions for the Owner

1. The new name (or the shortlist criteria).
2. Fresh public repository, or rewrite this one?
3. Which licence?
4. Does the sudo posture block publishing, or ship with a clear warning?
5. Keep `cb` as the command name?

## What is left to the builder

How the migration script is structured, how long the old environment variable
keeps working, and the order of the renames inside the migration.

## Constraints

- Depends on smooth updates (migration hook, `--check`, adopt) being done.
- Same safety rules as every change: no SSH, firewall or sudoers edits, no
  force-push to backup or archive remotes, no private names in any file,
  test or commit message.

## Done when

- Every existing install can move to the new name through the updater with
  one Owner yes, and `cb doctor` is clean afterwards.
- A written readiness decision exists, every blocker in it is closed or
  accepted by the Owner in writing, and the public repository (if the answer
  is yes) passes the name scan from a fresh clone.
