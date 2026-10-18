#!/usr/bin/env bash
# One-time, as root on the VM, after root can log in with a key:
#   bash harden.sh "<public key>" ["<public key>" ...]
# Creates the `deploy` user (CI and push.sh log in as it), lets it run only namma-deploy as root,
# turns SSH into keys-only, installs fail2ban and sets the clock to Bangkok time.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: harden.sh '<ssh public key>' ..." >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)

timedatectl set-timezone Asia/Bangkok

install -m 755 -o root -g root "$HERE/namma-deploy.sh" /usr/local/bin/namma-deploy

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
printf '%s\n' "$@" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys

echo 'deploy ALL=(root) NOPASSWD: /usr/local/bin/namma-deploy' > /etc/sudoers.d/namma-deploy
chmod 440 /etc/sudoers.d/namma-deploy
visudo -cf /etc/sudoers.d/namma-deploy

# Keys only. 10- sorts before the image's 50-cloud-init.conf, and sshd keeps the first value it reads.
cat > /etc/ssh/sshd_config.d/10-namma.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl reload ssh
sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin)'

DEBIAN_FRONTEND=noninteractive apt-get install -y -q fail2ban
systemctl enable --now fail2ban

systemctl restart namma     # pick up the new timezone
echo "harden done"
