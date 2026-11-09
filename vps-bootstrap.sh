#!/usr/bin/env bash
# VPS bootstrap for Claude Code — Debian 13 (trixie). Run ONCE as root.
#   bash vps-bootstrap.sh
# Optional overrides:  NEW_USER=claude SSH_PORT=22 bash vps-bootstrap.sh
set -euo pipefail

NEW_USER="${NEW_USER:-claude}"
SSH_PORT="${SSH_PORT:-22}"
AUTHORIZED_KEYS=(
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGI4hHPex6Vxi7hgxzwwuQr5sb1zT6rWVpVVVRer4tJW dev@infodevsec.com"
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBp80c39t0jKRrQsYdJKS67s5cekvnAntHtU1wkJTw5G claude-agent@newvps"
)

[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
step() { echo -e "\n==> $*"; }

step "Updating system"
apt-get update
apt-get -y full-upgrade
apt-get install -y sudo curl wget git ca-certificates gnupg ufw fail2ban python3-systemd \
  unattended-upgrades apt-listchanges ripgrep jq htop tmux unzip build-essential \
  python3 python3-venv python3-pip

step "Creating user '$NEW_USER' with passwordless sudo"
id "$NEW_USER" &>/dev/null || adduser --disabled-password --gecos "" "$NEW_USER"
usermod -aG sudo "$NEW_USER"
echo "$NEW_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$NEW_USER"
chmod 440 "/etc/sudoers.d/90-$NEW_USER"
visudo -cf "/etc/sudoers.d/90-$NEW_USER"

step "Installing SSH keys"
HOME_DIR="/home/$NEW_USER"
install -d -m 700 -o "$NEW_USER" -g "$NEW_USER" "$HOME_DIR/.ssh"
printf '%s\n' "${AUTHORIZED_KEYS[@]}" > "$HOME_DIR/.ssh/authorized_keys"
chown "$NEW_USER:$NEW_USER" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

step "Adding swap if RAM < 4 GB (Claude Code wants 4 GB+)"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $MEM_MB -lt 3900 ]] && ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "vm.swappiness=10" > /etc/sysctl.d/99-swap.conf && sysctl -p /etc/sysctl.d/99-swap.conf
fi

step "Firewall (ufw): allow SSH on $SSH_PORT only"
ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT/tcp"
ufw --force enable

step "fail2ban for SSH"
cat > /etc/fail2ban/jail.local <<JAIL
[sshd]
enabled  = true
backend  = systemd
port     = $SSH_PORT
maxretry = 5
bantime  = 1h
JAIL
systemctl enable --now fail2ban
systemctl restart fail2ban

step "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<AUTO
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AUTO
systemctl enable --now unattended-upgrades

step "Installing Claude Code for '$NEW_USER'"
sudo -u "$NEW_USER" -H bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
grep -q '.local/bin' "$HOME_DIR/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME_DIR/.bashrc"
sudo -u "$NEW_USER" -H mkdir -p "$HOME_DIR/projects"
cat > "$HOME_DIR/CLAUDE.md" <<MD
# This server
- Debian 13 VPS, managed by Claude Code. Admin user: $NEW_USER (passwordless sudo).
- Firewall: ufw (only SSH $SSH_PORT/tcp open). Open ports with \`sudo ufw allow <port>/tcp\` and note them here.
- fail2ban protects SSH. Unattended security upgrades are on.
- Projects live in ~/projects.
- Before risky changes (sshd, ufw, deleting data) explain the change and confirm with Navid.
- Never disable key-only SSH or remove ufw's SSH rule.
MD
chown "$NEW_USER:$NEW_USER" "$HOME_DIR/CLAUDE.md"

step "Hardening SSH (key-only, no root login) — applied LAST"
cat > /etc/ssh/sshd_config.d/10-hardening.conf <<SSHD
Port $SSH_PORT
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 4
AllowUsers $NEW_USER
SSHD
sshd -t
if systemctl is-active --quiet ssh.socket; then
  systemctl disable --now ssh.socket; systemctl enable ssh.service
fi
systemctl restart ssh

echo -e "\n✅ Done. KEEP THIS ROOT SESSION OPEN and test in a new terminal:"
echo "   ssh -i ~/.ssh/id_ed25519 -p $SSH_PORT $NEW_USER@<server-ip>"
echo "Then run 'claude' there once to log in with your Pro/Max account."
