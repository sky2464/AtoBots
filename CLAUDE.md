# ClaudeBot

Engine for a company of Claude Code bots on a private VPS. Owner talks to Admin; Admin delegates work.

## What this repo is

- `engine/` — the `cb` CLI, dispatcher, backup, archive, routines, doctor
- `tests/` — unittest suite (`python3 -m unittest discover -s tests`)
- `docs/` — design, plans, acceptance notes, [deploy](docs/deploy.md), [UPDATING.md](UPDATING.md)
- `vps-bootstrap.sh` — one-time Debian bootstrap (keys from argv / `SSH_PUBKEYS_FILE`, never hard-coded)

Company data lives in `CLAUDEBOT_HOME` (the company home), not in this repo. Secrets stay in `.secrets/` on the server and are never backed up.

## Owner ↔ Admin

- **Terminal:** `cb chat` — interactive Admin in the current SSH session.
- **Always-on:** `claudebot-admin.service` runs Admin in tmux (`admin-live`); optional Telegram after `cb secret add telegram-bot` and pairing (see install.sh messages).
- **New bots:** conversation with Admin; Soul/Persona approved before `cb bot new`.
- **Models:** new Admin installs default to **`sonnet`** (see `ADMIN_MODEL` in `engine/claudebot/cli.py`). The Admin template documents `cb bot set admin --model …` for opus/haiku.

## Cost-control CLI (v0.0.1+)

- **`cb usage`** — ledger of tokens per run; flags surprising model choices vs each bot's configured model.
- **`cb task send` / `cb task finish`** — thinkers delegate to worker-tier bots; dispatcher wakes workers and returns results.
- **`cb question open` / `close`** — register Owner questions; bare acknowledgements are dropped unless a question is open.

## Deploy and update

- Server keeps a **full Git clone** of this repo under e.g. `~/ClaudeBot`.
- Ship releases with annotated `v*` tags on `main`, then on the server: `engine/update.sh` (see [docs/deploy.md](docs/deploy.md), [UPDATING.md](UPDATING.md)).
- Do **not** rsync with `--delete` into the checkout; that breaks `update.sh` and can remove `.git`.
- `engine/install.sh` sets up the company home, systemd units, and `cb` on PATH.

## Host inventory

Do not put server IP, hostname, SSH key paths, Telegram bot names, or deploy-key details in this file. Keep that in an Owner-only note or fill in `docs/templates/server-inventory.md` outside the engine history if needed.

## Bootstrap (once, as root on a fresh VPS)

```bash
bash vps-bootstrap.sh /path/to/owner.pub /path/to/agent.pub
# or: SSH_PUBKEYS_FILE=/path/to/keys.txt bash vps-bootstrap.sh
```

Overrides: `NEW_USER=claude SSH_PORT=22`. The script merges into `authorized_keys` (does not truncate), and when RAM is under 4 GB ensures total swap is at least 4 GB even if a small provider swap already exists.

## Rules

- Never lock us out: don't change sshd/ufw without keeping an existing session open and testing a second login.
- Never paste or request the root password in chat.
- Explain risky changes (sshd, firewall, deleting data) and confirm with the Owner first.
- Backup and archive remotes must never be force-pushed; the engine refuses `--force` on those pushes.
