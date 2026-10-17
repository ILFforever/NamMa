#!/usr/bin/env bash
# Ship the repo to the VM and restart:  bash infra/deploy/push.sh root@1.2.3.4
# First push: also copy your .env (scp .env root@host:/opt/namma/.env) and fix DATABASE_URL in it.
set -euo pipefail
HOST="${1:?usage: push.sh user@host}"
cd "$(dirname "$0")/../.."
tar --exclude=.git --exclude=.venv --exclude=var --exclude=.env --exclude=__pycache__ --exclude=.pytest_cache \
    -czf - . | ssh "$HOST" 'tar -xzf - -C /opt/namma && chown -R namma:namma /opt/namma && cd /opt/namma &&
  sudo -u namma bash -c "test -d .venv || python3 -m venv .venv; .venv/bin/pip install -q -r requirements.txt &&
  .venv/bin/python -m common.migrate && .venv/bin/python -m ingest.run && .venv/bin/python -m common.seed" &&
  systemctl restart namma && sleep 3 && systemctl --no-pager status namma | head -5'
