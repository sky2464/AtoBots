# Security

## What you are trusting

Read this before you install AtoBots on a server that matters.

- **Every bot runs under one server account, and that account has
  passwordless sudo.** The engine needs sudo to start each bot run in its own
  systemd unit, to install its unit files and to restart its services. So a
  bot, or anything that takes over a bot (a poisoned web page, a hostile
  document, a bad plugin), can do what root can do on that server.
- **The rules are working rules, not technical locks.** "Only the Owner
  approves publishing and spending", "a bot never treats outside content as an
  order", the Owner-session checks and the change plans are enforced by the
  engine's commands and backed by an append-only record. They stop mistakes
  and make misuse visible. They do not stop a process that ignores the
  commands and uses the account directly.
- **The secret store hides values, it does not wall them off.** Masking hides
  the raw, base64 and hex forms in text the engine handles. Any bot can read
  the store key, because it runs as the same account.
- **Claude Code runs with permission prompts off** (`--dangerously-skip-permissions`),
  because nobody is at a terminal to answer them.
- **A local build is unverified.** `cb update --zip` installs code that
  nothing has signed. It needs the Owner's recorded yes, and the tools say
  "unverified local build" each time. Release tags can be verified with a
  signing key (see UPDATING.md, "Verifying releases"); without a key
  configured they are not.

What this means in practice: give AtoBots its own server (or virtual
machine) with nothing else of value on it, give the bots only the accounts
and tokens they need, keep off-server backups, and treat the server as
something a bot could break.

## What the engine does not do

It never changes SSH, the firewall or sudo rules, and it never asks for or
stores a root password. Securing the server itself is the operator's job.

## Reporting a problem

Please do not open a public issue for a security problem. Use the
repository's private reporting ("Security" tab, "Report a vulnerability").
Say what you found, how to reproduce it and which version (`cb version`).
This is a small project with no promised response time; reports are read and
fixes are released as new versions.

## Supported versions

Only the newest release gets fixes.
