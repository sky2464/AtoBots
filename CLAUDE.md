# ClaudeBot — VPS for Claude Code

Owner: Navid (Atoosa). Goal: a fresh VPS that Claude Code is installed on and manages.

## Server
- Provider: RackNerd, hostname `racknerd-5fc4050`
- IP: 192.210.190.169, SSH port 22
- Debian 13 (trixie) 64-bit, 2 vCPU, 2 GB RAM, 35 GB disk
- Freshly reset on 2026-11-08 (all old data and the old `adminuser` are gone)
- Company home: `~/AtoosaDev` (company Atoosa Dev; fresh start 2026-11-09, the old `~/Atoosa` test home was archived to `backups/` on the Mac and deleted). Line of business: DecisionTreeByNavid (YouTube channel)
- Engine repo: github.com/sky2464/claudebot (private). Backups: `atoosa-hub/backup` (hourly push, remote `backup`) and `atoosa-hub/archive` (remote `archive`). The `backup` deploy key is pending (Owner adds it in GitHub); until then the first push fails
- Always-on Admin with Telegram @AtoosaHub_Bot via `claudebot-admin.service` (tmux `admin-live`)
- Engine: `~/ClaudeBot` (`engine/`); `cb` is linked into `/usr/local/bin`, `CLAUDEBOT_HOME=/home/claude/AtoosaDev` (also in `/etc/claudebot.env`)
- Navid talks to Admin with `cb chat`

## Access from this Mac
- SSH alias: `ssh atoosa-vps` → user `claude`, key `~/.ssh/claude_vps_ed25519` (dedicated key for Claude; revoke it by deleting its line in the server's `~/.ssh/authorized_keys`)
- Navid's personal key `~/.ssh/id_ed25519` is also authorized for user `claude`
- `~/.ssh/config` was backed up as `~/.ssh/config.bak-20261108` before the `atoosa-vps` entry was added
- Old entries `vps2` and `racknerd` in `~/.ssh/config` point to the same IP but are stale (user `adminuser` / port 4422 no longer exist). Ask Navid before removing them.
- The old host key for 192.210.190.169 was removed from `known_hosts` and the new ed25519 key was added.

## Always-on Admin with Telegram (set up 2026-11-09)
- Runs as `claudebot-admin.service` (starts at boot once enabled), inside tmux session `admin-live`, as `cb admin serve`: Admin with `--channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions`. It does not pause bots (only `cb chat` does). State is in `~/AtoosaDev/.admin-live/`.
- Attach: `ssh atoosa-vps` then `tmux attach -t admin-live`. Detach without stopping it: Ctrl-b then d. Never press Ctrl-c or type `/exit` unless you want it restarted (it restarts after 10 s).
- Check: `systemctl status claudebot-admin`, `tmux ls`, `cb doctor`.
- Stop: `sudo systemctl stop claudebot-admin` (also ends the tmux session). Disable at boot: `sudo systemctl disable claudebot-admin`.
- The Telegram plugin is installed at local scope for `~/AtoosaDev` (bun in `~/.bun/bin`; it is enabled for the session by `--settings`, not in any file); access lives in `access.json` next to the `.env`. The bot token lives only in the secure store (`cb secret add telegram-bot --kind token`, typed at a hidden prompt, never through a Claude session). At each start `cb admin serve` writes it into the plugin's own `~/.claude/channels/telegram/.env` (mode 600, outside the company home, so never in Git or the backup) and removes the file when the session ends or the service stops; each use is recorded by name. Pairing and allowlist changes are made only in the attached terminal.

## vps-bootstrap.sh (in this folder) — run once as root on the server
Does: full-upgrade + tools (git, curl, ripgrep, jq, tmux, build-essential, python3), creates user `claude` with passwordless sudo,
installs both public keys, adds 4 GB swap (RAM < 4 GB), ufw (only SSH open), fail2ban (systemd backend), unattended-upgrades,
installs Claude Code (native installer) for `claude`, writes `~/CLAUDE.md` on the server, then hardens SSH LAST
(no root login, no passwords, `AllowUsers claude`). Override with `NEW_USER=... SSH_PORT=... bash vps-bootstrap.sh`.

## Status / next steps
1. [x] Bootstrap script run on the server (2026-11-08 CST)
2. [x] `ssh atoosa-vps` verified from a second terminal
3. [x] Claude Code logged in on the server (`~/.claude/.credentials.json` written 2026-11-09 UTC). Navid runs it from `~/ClaudeBot` with `--dangerously-skip-permissions`
4. [x] Verified remotely: ufw (SSH only), fail2ban, unattended-upgrades, Claude Code 2.1.319, sshd hardening; root login is refused from outside
5. [x] Rebooted onto kernel 6.12.116; all of item 4 and the 4 GB of swap re-checked after boot
6. [ ] Optional: remove stale `vps2` / `racknerd` SSH entries (with Navid's OK)

## Known server quirks
- Swap: the RackNerd image ships a 1 GB partition (`/dev/vda2`), so the script's "only if no swap exists" check skipped its 4 GB file. Added `/swapfile` (3 GB) by hand for 4 GB total, plus `vm.swappiness=10`. `/etc/fstab.bak-20261109` is the backup from before that edit. On a fresh reset the script will skip swap again.
- `networking.service` failed on the first boot (`ifup`: "Address already assigned" on eth0) but was active after the 2026-11-09 reboot, so it may come and go. eth0 appears in both `/etc/network/interfaces` and `/etc/netplan/50-ctrl.yaml`, both from the provider's image. The network worked either way, so a failure here alone is not a problem.

## Decisions
- Claude has full control of this server by design: always on, private use, `--dangerously-skip-permissions` with passwordless sudo. Navid declined extra guardrails on sshd/ufw (2026-11-08 CST). The Rules below still apply.

## Rules
- Never lock us out: don't change sshd/ufw without keeping an existing session open and testing a second login.
- Never paste or request the root password in chat.
- Explain risky changes (sshd, firewall, deleting data) and confirm with Navid first.
