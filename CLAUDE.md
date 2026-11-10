# ClaudeBot

Engine for a company of Claude Code bots. The Owner talks to Admin; Admin delegates work.

## What this repo is

- `engine/` — the `cb` CLI, dispatcher, backup, archive, routines, doctor
- `tests/` — unittest suite (`python3 -m unittest discover -s tests`)
- `docs/` — design

Company data lives in `CLAUDEBOT_HOME` (the company home), not in this repo. Secrets stay in `.secrets/` on the server and are never backed up.

## Host inventory

Do not put server IP, hostname, SSH key paths, Telegram bot names, or deploy-key details in this file. Keep that in an Owner-only note or fill in `docs/templates/server-inventory.md` outside the engine history if needed.

## Rules

- Never lock the Owner out: don't change remote login or the firewall without keeping an existing session open and testing a second login.
- Never paste or request the root password in chat.
- Explain risky changes (remote login, the firewall, deleting data) and confirm with the Owner first.
- Backup and archive remotes must never be force-pushed; the engine refuses `--force` on those pushes.
