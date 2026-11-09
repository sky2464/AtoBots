# ClaudeBot — VPS for Claude Code

Owner: Navid (Atoosa). Goal: a fresh VPS that Claude Code is installed on and manages.

## Server
- Provider: RackNerd, hostname `racknerd-5fc4050`
- IP: 192.210.190.169, SSH port 22
- Debian 13 (trixie) 64-bit, 2 vCPU, 2 GB RAM, 35 GB disk
- Freshly reset on 2026-11-08 (all old data and the old `adminuser` are gone)

## Access from this Mac
- SSH alias: `ssh atoosa-vps` → user `claude`, key `~/.ssh/claude_vps_ed25519` (dedicated key for Claude; revoke it by deleting its line in the server's `~/.ssh/authorized_keys`)
- Navid's personal key `~/.ssh/id_ed25519` is also authorized for user `claude`
- `~/.ssh/config` was backed up as `~/.ssh/config.bak-20261108` before the `atoosa-vps` entry was added
- Old entries `vps2` and `racknerd` in `~/.ssh/config` point to the same IP but are stale (user `adminuser` / port 4422 no longer exist). Ask Navid before removing them.
- The old host key for 192.210.190.169 was removed from `known_hosts` and the new ed25519 key was added.

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
