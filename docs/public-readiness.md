# Readiness to be public

Written 2026-11-10, with release 0.1.0, and brought up to date for 0.2.1.
Each line says what was checked, what was found, and who decides.

**Decision: not yet. Ready after the four Owner items below.** The code, the
name and the update path are ready. Nothing in this list was changed on the
hosted repository: its visibility, settings and history are as they were.

## Open items for the Owner (blocking)

| # | Item | Why it blocks | Recommendation |
|---|------|---------------|----------------|
| 1 | **History** | Earlier commits and the `v0.0.1` tag held one server's address, SSH key paths and company names. A scan of this repository's history on the date above still finds one address-shaped string outside the tests (not confirmed to be the server's) and twelve commits that touch SSH key paths. The current tree is clean; the history is not. | Publish a **new repository from a clean export** of the `v0.2.1` tree (no history). Keep this one private. Nothing needs rewriting, and no old tag comes along. |
| 2 | **Keys** | Paths, not key contents, were in the history, but the server address was there too. | Rotate the deploy keys and review who had read access to the private repository. The Owner's call. |
| 3 | **Licence** | There is no licence file. Without one, nobody may legally use or change the code. | The Owner chooses. Nothing was picked in the build. |
| 4 | **Name checks** | "AtoBots" was chosen by the Owner. It does not contain Anthropic's marks, which was the problem with the old name. Not checked: whether the name is free as a GitHub organisation or repository, as a package or domain name, or as a trademark in the Owner's market. | Run those three searches before the first public push. A lawyer's view is the Owner's call. |

## Done in 0.1.0

- **The name.** The package, the services, the variable, the env file and the
  docs use the new name everywhere, and a test fails if an earlier name shows
  up in any tracked file (0.2.1 removed the compatibility layer).
- **Existing installs.** The updater backs up, restarts and rolls back if
  any step fails. Installs older than 0.1.0 can no longer be updated in
  place.
- **Wording about Anthropic's product.** The README says the project "works
  with Claude Code", that the operator installs and logs in to it, and that
  the project is independent and not endorsed. "Claude Code" is used only to
  name the product the engine needs.
- **Security posture stated plainly.** [SECURITY.md](../SECURITY.md) says
  that every bot runs under one account with passwordless sudo, that the
  rules are not technical locks, and how to report a problem.
- **The command name.** `cb` stays. It is short, it is not the product name,
  and every bot's skills and memory use it. (My call; easy to revisit.)

## Decisions still open (not blocking the code)

- **Does the sudo posture block publishing?** It is unchanged: narrowing sudo
  to the few commands the engine needs was left as an Owner decision in an
  earlier review. Recommendation: publish with the warning in SECURITY.md,
  and narrow sudo as its own release.
- **Dependencies.** The always-on Admin needs Claude Code and its Telegram
  channel plugin. The README lists the requirements. Not checked: whether the
  plugin's terms allow this use, and which versions are the minimum. Someone
  should read the terms before publishing.
- **Design history in `docs/`.** The specs and the review under
  `docs/superpowers/` and `docs/review/` describe internal
  decisions. Recommendation: leave them out of the public export, or
  keep only the two engine design specs.

## Repository settings (to do on the public repository)

- CI on every push and pull request: in place (tests, name scan of commit
  messages, shellcheck).
- The name scan reads its word list from the `CB_BANNED_TERMS` secret. Set it
  on the new repository, or the scan is skipped on pull requests and fails on
  `main`.
- Branch protection on `main` and secret scanning: not available on this
  private repository's plan; both are free on a public one. Turn them on at
  creation.
- Private vulnerability reporting: turn it on (SECURITY.md points to it).

## Docs for strangers

The README covers what it is, requirements, install, the commands, updating
and the safety rules. Still missing for a first-time reader: how to
uninstall, what it costs to run, and a short "first company" walk-through.

## Support boundary

Not decided. SECURITY.md promises only that reports are read. Say in the
README what the Owner will and will not answer before inviting issues.
