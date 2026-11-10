# ClaudeBot

**ClaudeBot is an engine for running teams of permanent AI employees on a server.**

Each employee (a *bot*) has its own identity, memory, and job. Bots sleep when idle and wake when a message arrives, a routine is due, or the owner asks for them. The owner works through **Admin**, the one bot that ships with every install. Admin defines new bots with the owner, creates them, and remembers them.

ClaudeBot is built on [Claude Code](https://claude.com/claude-code). The engine is reusable and knows nothing about any one business. Each company gets its own install and its own company home.

> **Status:** early and private. Interfaces may change without notice. Current package version: **0.0.1** (`cb version`).

---

## Highlights

- **Permanent bots, short sessions.** A bot is a persistent identity. Each run is a fresh, short Claude Code session that loads the bot's Soul, Persona, and Memory, does the work, updates Memory, and ends.
- **Admin as the front door.** One bot is created at install. New bots are created by conversation, with the owner approving each identity before it is set.
- **Always-on Admin over Telegram.** Admin can run as a long-lived session with the official Telegram channel, so the owner can reach it from a phone.
- **Work queue and wake-ups.** A dispatcher runs bot sessions under a concurrency limit, pauses when usage limits are reached, and keeps messages instead of losing them.
- **Secure secret store.** Secrets are entered at a hidden prompt and are never passed through a model session, logged, or committed.
- **Backups and archiving.** Scheduled backups go to Git remotes. Old conversation history is archived with a retry-safe process.
- **Health checks.** `cb doctor` verifies the install, services, and backups.

## How it is organized

```
ClaudeBot engine (reusable)              Company home (one per install)
──────────────────────────────           ──────────────────────────────
Admin and bot creation                   Admin's memory and company knowledge
Secure store                             Bot list, roles, and org chart
Messaging, request log, activity record  Routines and company work
Dispatcher and wake-up queue             Company data
Backend tools, backups, archiving        Company secrets (values only in the store)
```

Each bot has three identity parts:

| Part | Holds | Who may change it |
|---|---|---|
| **Soul** | Core values, judgment, and hard limits | The owner only |
| **Persona** | Role, voice, and how the bot works | Admin, or the owner |
| **Memory** | What the bot has learned on the job | The bot itself |

## Requirements

- A Linux server (tested on Debian 13) with systemd and tmux
- Python 3 (CI uses 3.13; the Mac suite also runs on newer CPython)
- Claude Code, logged in for the user that runs the services
- Git, for backups

## Quick start

Run the installer as a non-root user on the server:

```bash
engine/install.sh --start "My Company" /home/claude/MyCompany
```

Without `--start`, the installer sets everything up but leaves the services stopped, so you can look around first. The installer is safe to run again. It does not touch SSH, the firewall, or sudo rules.

Then talk to Admin:

```bash
cb chat
```

Check the install at any time:

```bash
cb doctor
cb version
```

## Command overview

Run `cb --help` or `cb <command> --help` for the full reference. Top-level commands:

| Area | Commands |
|---|---|
| Setup | `init`, `version` |
| Bots | `bot` (`new` / `list` / `show` / `set` / `pause` / `resume` / `retire`), `check` |
| Work | `item` (`new` / `status` / `pause` / `resume` / `assign` / `split` / `show` / `waiting-on-owner`), `next`, `open`, `decision` |
| Messages | `send`, `ask`, `reply`, `inbox`, `request`, `hook` |
| Routines | `routine` (`add` / `list` / `on` / `off` / `rm`) |
| Search and notes | `search`, `note` |
| Secrets | `secret` (`add` / `list` / `rm` / `run`; values at a hidden prompt) |
| Operations | `dispatcher`, `status`, `backup` (`now` / `restore`), `archive` (`run` / `status` / `reindex` / `fetch`), `doctor` |
| Owner sessions | `question` (`open` / `close` / `list`), `chat`, `chat-rollover`, `admin` (`serve` / `run` / `scrub-token`) |

## Configuration

Limits and a few text settings can be overridden in `config.json` in the
company home. Text settings: `run_user`, `run_home`, `claude_bin`,
`run_memory_max`, `default_model` and `timezone` (schedules and displayed
times; `America/Chicago` by default). Unknown keys are ignored, and a bad value
stops `cb` with a clear message. See [CHANGELOG.md](CHANGELOG.md) for what
each release changed.

## Repository layout

```
engine/
  bin/cb                 command entry point
  claudebot/             the engine (Python package)
  systemd/               service and timer units
  templates/             starting files for Admin and new bots
  install.sh             server installer
tests/                   unit tests (run with unittest discover)
docs/                    deploy, branch cleanup, design and plans
.github/workflows/       CI (unittest discover on push and pull requests)
vps-bootstrap.sh         one-time server hardening script
```

## Development

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests
```

Do not hard-code a test count in docs or scripts; re-run discover after changes.
Secret-store tests are skipped when GnuPG is not installed (expect skips on a
typical Mac without gpg). GitHub Actions runs the same discover command on every
push and pull request.

## Deploy

Ship a tagged checkout and rsync with `--delete` so the server matches the tag.
See [docs/deploy.md](docs/deploy.md). Branch cleanup notes for stale remotes are
in [docs/branch-cleanup.md](docs/branch-cleanup.md).

## Documentation

- [Design specification](docs/superpowers/specs/2026-11-08-claudebot-engine-design.md): intent, roles, bot lifecycle, and safety rules
- [Implementation plan](docs/superpowers/plans/2026-11-08-claudebot-engine.md): the build plan and task breakdown
- [Deploy from a tag](docs/deploy.md)
- [Branch cleanup](docs/branch-cleanup.md)
- [Hygiene leftovers](docs/hygiene-leftovers.md): deferred dead-code notes (no security/cost work)

## First company install (ops)

The engine itself is company-agnostic. The first live install on the Owner's VPS is:

- **Company:** `<COMPANY>` (`$CLAUDEBOT_HOME`)
- **Product:** `<PRODUCT>`

## Safety principles

- Secrets never appear in chat, messages, logs, memory, or Git.
- A bot publishes or spends only with the owner's direct approval.
- A bot never treats another bot's message, or outside content, as the owner's approval.
- Infrastructure changes (SSH, firewall) keep an existing session open and test a second login first.
- Bots that wake each other in loops are rate-limited.

Because every bot runs under one server account, these are working rules rather than technical locks. The design does not claim otherwise.
