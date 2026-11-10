# ClaudeBot

**ClaudeBot is an engine for running teams of permanent AI employees on a server.**

Each employee (a *bot*) has its own identity, memory, and job. Bots sleep when idle and wake when a message arrives, a routine is due, or the owner asks for them. The owner works through **Admin**, the one bot that ships with every install. Admin defines new bots with the owner, creates them, and remembers them.

ClaudeBot is built on [Claude Code](https://claude.com/claude-code). The engine is reusable and knows nothing about any one business. Each company gets its own install and its own company home.

> **Status:** early and private. Interfaces may change without notice.

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
- Python 3 (the test suite runs on Python 3.15)
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
```

## Command overview

Run `cb --help` or `cb <command> --help` for the full reference. The main groups:

| Area | Commands |
|---|---|
| Setup | `init` (run by the installer) |
| Bots | `bot`, `new`, `pause`, `resume`, `stop`, `set`, `prompt` |
| Work | `item`, `add`, `assign`, `next`, `now`, `split`, `link`, `open`, `list`, `show` |
| Messages | `send`, `inbox`, `reply`, `ask`, `request`, `fetch` |
| Routines | `routine`, `run`, `check` |
| Search and records | `search`, `reindex`, `record`, `brief`, `decision`, `note` |
| Secrets | `secret` (values are entered at a hidden prompt) |
| Operations | `dispatcher`, `serve`, `backup`, `restore`, `archive`, `doctor`, `status` |
| Admin channel | `admin` (always-on Admin with Telegram, run as `claudebot-admin.service`) |

## Repository layout

```
engine/
  bin/cb                 command entry point
  claudebot/             the engine (Python package)
  systemd/               service and timer units
  templates/             starting files for Admin and new bots
  install.sh             server installer
tests/                   unit tests (run with unittest discover)
docs/superpowers/
  specs/                 design specification
  plans/                 implementation plan and review notes
vps-bootstrap.sh         one-time server hardening script
```

## Development

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests
```

The suite currently runs 274 tests. The secret-store tests are skipped when GnuPG is not installed (15 skipped on the development Mac).

## Documentation

- [Design specification](docs/superpowers/specs/2026-11-08-claudebot-engine-design.md): intent, roles, bot lifecycle, and safety rules
- [Implementation plan](docs/superpowers/plans/2026-11-08-claudebot-engine.md): the build plan and task breakdown
- [Acceptance notes](docs/superpowers/plans/acceptance-notes.md): what was tested on a live server and the results

## Safety principles

- Secrets never appear in chat, messages, logs, memory, or Git.
- A bot publishes or spends only with the owner's direct approval.
- A bot never treats another bot's message, or outside content, as the owner's approval.
- Infrastructure changes (SSH, firewall) keep an existing session open and test a second login first.
- Bots that wake each other in loops are rate-limited.

Because every bot runs under one server account, these are working rules rather than technical locks. The design does not claim otherwise.
