#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 ECS VM. Run as root:  NAMMA_DOMAIN=1.2.3.4.sslip.io bash setup_ecs.sh
# Postgres runs on the same VM (cheapest). For RDS instead, skip the postgres lines and point
# DATABASE_URL in /opt/namma/.env at the RDS instance.
set -euo pipefail
: "${NAMMA_DOMAIN:?set NAMMA_DOMAIN, e.g. 1.2.3.4.sslip.io}"

apt-get update
apt-get install -y python3-venv python3-pip postgresql debian-keyring debian-archive-keyring apt-transport-https curl gpg

# Caddy (official apt repo)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

id namma >/dev/null 2>&1 || useradd --system --home /opt/namma --shell /usr/sbin/nologin namma
mkdir -p /opt/namma && chown namma:namma /opt/namma

# Local Postgres, reachable only from localhost.
DB_PASS=$(openssl rand -hex 16)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='namma'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE ROLE namma LOGIN PASSWORD '${DB_PASS}'"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='namma'" | grep -q 1 || \
  sudo -u postgres createdb -O namma namma
echo "DATABASE_URL=postgresql://namma:${DB_PASS}@localhost:5432/namma" > /root/namma_db_url.txt

echo "NAMMA_DOMAIN=${NAMMA_DOMAIN}" > /etc/default/caddy
cp "$(dirname "$0")/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/default/caddy\n' > /etc/systemd/system/caddy.service.d/env.conf
cp "$(dirname "$0")/namma.service" /etc/systemd/system/namma.service
systemctl daemon-reload
systemctl enable caddy namma
systemctl restart caddy

echo "Done. Next: push code with infra/deploy/push.sh, put DATABASE_URL from /root/namma_db_url.txt into /opt/namma/.env"
