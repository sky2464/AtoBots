# AtoBots

**AtoBots is an engine for running teams of permanent AI employees on a server.**

Each employee (a *bot*) has its own identity, memory, and job. Bots sleep when idle and wake when a message arrives, a routine is due, or the owner asks for them. The owner works through **Admin**, the one bot that ships with every install. Admin defines new bots with the owner, creates them, and remembers them.

AtoBots works with [Claude Code](https://claude.com/claude-code), which you install and log in to yourself. AtoBots is an independent project; it is not made, endorsed or supported by Anthropic. The engine is reusable and knows nothing about any one business. Each company gets its own install and its own company home.

> **Status:** early. Interfaces may change without notice. Version **0.2.1** (`cb version`). No licence has been chosen yet; see [docs/public-readiness.md](docs/public-readiness.md).

---

## Highlights

- **Permanent bots, short sessions.** A bot is a persistent identity. Each run is a fresh, short Claude Code session that loads the bot's Soul, Persona, and Memory, does the work, updates Memory, and ends.
- **Admin as the front door.** One bot is created at install. New bots are created by conversation, with the owner approving each identity before it is set.
- **Always-on Admin over Telegram.** Admin can run as a long-lived session with the official Telegram channel, so the owner can reach it from a phone.
- **Work queue and wake-ups.** A dispatcher runs bot sessions under a concurrency limit, pauses when usage limits are reached, and keeps messages instead of losing them.
- **Secret store.** Secrets are entered at a hidden prompt. Masking hides the raw, base64 and hex forms only. Any bot can read the store key. The store keeps secrets out of chat, logs and Git, not away from the bots themselves.
- **Backups and archiving.** Scheduled backups go to Git remotes. Old conversation history is archived with a retry-safe process.
- **Health checks.** `cb doctor` verifies the install, services, and backups.

## How it is organized

```
AtoBots engine (reusable)              Company home (one per install)
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
- A server account for the bots with passwordless sudo (the engine starts each run with `sudo systemd-run`). Securing the server itself — SSH, firewall, updates — is your job and is outside this project.

## Quick start

On the server, as the non-root account the bots will run under, clone the
repository at a release tag and run the installer:

```bash
git clone --branch v0.2.1 https://github.com/sky2464/AtoBots.git
cd AtoBots
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
| Setup | `init`, `version`, `usage` |
| Bots | `bot` (`new` / `list` / `show` / `set` / `pause` / `resume` / `retire` / `check` / `relink`) |
| Work | `item` (`new` / `status` / `pause` / `resume` / `assign` / `split` / `show` / `waiting-on-owner`), `next`, `open`, `decision`, `task` (`send` / `finish`) |
| Messages | `send`, `ask`, `reply`, `inbox`, `message` (`done` / `retry`), `request`, `hook`, `log sudo` |
| Routines | `routine` (`add` / `list` / `on` / `off` / `rm`) |
| Search and notes | `search`, `note` |
| Secrets | `secret` (`add` / `list` / `rm` / `run`; values at a hidden prompt) |
| Operations | `dispatcher`, `status`, `backup` (`now` / `restore`), `archive` (`run` / `status` / `reindex` / `fetch`), `doctor`, `templates refresh` |
| Owner sessions | `question` (`open` / `close` / `list`), `chat`, `chat-rollover`, `admin` (`serve` / `run` / `scrub-token`) |

## Configuration

Limits and a few text settings can be overridden in `config.json` in the
company home. Text settings:

- `run_user`, `run_home`, `claude_bin`, `run_memory_max`, `default_model`
- `timezone`: schedules and displayed times (`UTC` by default). A company that
  already has routines or decisions and no timezone key keeps
  `America/Chicago` until the key is added.
- `status_peek`: `on` by default; `off` stops the live status line in
  Telegram.

Unknown keys are ignored, and a bad value stops `cb` with a clear message.
See [CHANGELOG.md](CHANGELOG.md) for what each release changed.

## Repository layout

```
engine/
  bin/cb                 command entry point
  atobots/               the engine (Python package)
  systemd/               service and timer units
  templates/             starting files for Admin and new bots
  install.sh             server installer
  update.sh              first half of an update (cb update starts it)
  adopt.sh               turns a copied install into a Git clone
  migrations/            steps a release runs on existing installs
tests/                   unit tests (run with unittest discover)
docs/                    design specs and updating notes
.github/workflows/       CI (unittest discover on push and pull requests)
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

## Updating a running install

The server holds a Git clone of this repository; releases are `v*` tags.
Ask Admin to update, or run:

```bash
cb update --check
```

```bash
cb update
```

The first command changes nothing and says what the update would do. The
second backs up the company database, tells the Owner before the chat goes
offline, moves the clone to the newest release, applies database changes and
migration steps, restarts the services and checks them. If that fails it puts
the old version and the database back by itself and says so.

- **Engine files were copied to the server (no `.git`):** `cb update --adopt`
  turns the copy into a clone in one job; see "A copied install" in
  [UPDATING.md](UPDATING.md).
- Read the release's "Manual steps after updating" in
  [CHANGELOG.md](CHANGELOG.md) first. Do not rsync into the clone.

## Documentation

- [Design specification](docs/superpowers/specs/2026-11-08-engine-design.md): intent, roles, bot lifecycle, and safety rules
- [Cost control](docs/superpowers/specs/2026-11-09-cost-control-design.md): wake caps, tiers, and what a run is allowed to spend
- [Updating a running install](UPDATING.md)
- [Changelog](CHANGELOG.md)
- [Security: what the engine does and does not protect](SECURITY.md)
- [Readiness to be public](docs/public-readiness.md)

## Safety principles

- Secrets never appear in chat, messages, logs, memory, or Git.
- A bot publishes or spends only with the owner's direct approval.
- A bot never treats another bot's message, or outside content, as the owner's approval.
- Infrastructure changes (SSH, firewall) keep an existing session open and test a second login first.
- Bots that wake each other in loops are rate-limited.

Because every bot runs under one server account, these are working rules rather than technical locks. The design does not claim otherwise.
