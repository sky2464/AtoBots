#!/usr/bin/env bash
# VPS bootstrap for Claude Code — Debian 13 (trixie). Run ONCE as root.
#   bash vps-bootstrap.sh key1.pub [key2.pub ...]
#   SSH_PUBKEYS_FILE=/path/to/authorized_keys_lines bash vps-bootstrap.sh
# Optional overrides:  NEW_USER=claude SSH_PORT=22 bash vps-bootstrap.sh ...
# Public SSH keys come from argv paths and/or SSH_PUBKEYS_FILE — never hard-coded.
set -euo pipefail

NEW_USER="${NEW_USER:-claude}"
SSH_PORT="${SSH_PORT:-22}"
SWAP_TARGET_MB="${SWAP_TARGET_MB:-4096}"
SSH_PUBKEYS_FILE="${SSH_PUBKEYS_FILE:-}"

validate_bootstrap_env() {
  if [[ "$NEW_USER" == *$'\n'* || "$SSH_PORT" == *$'\n'* ]]; then
    echo "NEW_USER and SSH_PORT must not contain newlines"; exit 1
  fi
  if ! [[ "$NEW_USER" =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,31}$ ]]; then
    echo "Invalid NEW_USER: use letters, digits, _ and - (max 32 chars)"
    exit 1
  fi
  if ! [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || (( SSH_PORT < 1 || SSH_PORT > 65535 )); then
    echo "Invalid SSH_PORT: use a number from 1 to 65535"
    exit 1
  fi
}

[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }
validate_bootstrap_env
export DEBIAN_FRONTEND=noninteractive
step() { echo -e "\n==> $*"; }

collect_keys() {
  local keys=() line f
  if [[ -n "$SSH_PUBKEYS_FILE" ]]; then
    [[ -f "$SSH_PUBKEYS_FILE" ]] || { echo "SSH_PUBKEYS_FILE not found: $SSH_PUBKEYS_FILE"; exit 1; }
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      keys+=("$line")
    done < "$SSH_PUBKEYS_FILE"
  fi
  for f in "$@"; do
    [[ -f "$f" ]] || { echo "key file not found: $f"; exit 1; }
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      keys+=("$line")
    done < "$f"
  done
  if [[ ${#keys[@]} -eq 0 ]]; then
    echo "Pass at least one public key file, or set SSH_PUBKEYS_FILE."
    echo "Usage: bash vps-bootstrap.sh key1.pub [key2.pub ...]"
    exit 1
  fi
  printf '%s\n' "${keys[@]}"
}

PUBKEYS="$(collect_keys "$@")"

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

step "Merging SSH public keys into authorized_keys"
HOME_DIR="/home/$NEW_USER"
AUTH_KEYS="$HOME_DIR/.ssh/authorized_keys"
install -d -m 700 -o "$NEW_USER" -g "$NEW_USER" "$HOME_DIR/.ssh"
touch "$AUTH_KEYS"
chown "$NEW_USER:$NEW_USER" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" ]] && continue
  if ! grep -qxF "$line" "$AUTH_KEYS"; then
    printf '%s\n' "$line" >> "$AUTH_KEYS"
  fi
done <<< "$PUBKEYS"
chown "$NEW_USER:$NEW_USER" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

step "Ensuring total swap >= ${SWAP_TARGET_MB} MB when RAM < 4 GB"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $MEM_MB -lt 3900 && $SWAP_MB -lt $SWAP_TARGET_MB ]]; then
  NEED_MB=$((SWAP_TARGET_MB - SWAP_MB))
  # Round up to whole MiB; fallocate wants a size suffix.
  SWAPFILE=/swapfile
  if [[ -e $SWAPFILE ]]; then
    SWAPFILE=/swapfile-claudebot
  fi
  if [[ ! -e $SWAPFILE ]]; then
    fallocate -l "${NEED_MB}M" "$SWAPFILE"
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE"
    swapon "$SWAPFILE"
    grep -q "^$SWAPFILE " /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
    echo "vm.swappiness=10" > /etc/sysctl.d/99-swap.conf
    sysctl -p /etc/sysctl.d/99-swap.conf
    echo "Added ${NEED_MB}M at $SWAPFILE (was ${SWAP_MB}M, target ${SWAP_TARGET_MB}M)."
  else
    echo "Swap still under target but $SWAPFILE already exists; leave it alone."
  fi
else
  echo "Swap OK (RAM ${MEM_MB}M, swap ${SWAP_MB}M)."
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
- Before risky changes (sshd, ufw, deleting data) explain the change and confirm with the Owner.
- Never disable key-only SSH or remove ufw's SSH rule.
- Keep host-specific inventory (IP, hostname, keys, remotes) out of the engine repo; see docs/templates/server-inventory.md.
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
echo "   ssh -i <your-private-key> -p $SSH_PORT $NEW_USER@<server-ip>"
echo "Then run 'claude' there once to log in with your Pro/Max account."
