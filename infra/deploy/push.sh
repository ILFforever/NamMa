#!/usr/bin/env bash
# Ship this working tree (and the web build, if present) to the VM:  bash infra/deploy/push.sh deploy@1.2.3.4
# Logs in as the `deploy` user (see harden.sh) and hands the code to namma-deploy, which installs, migrates,
# restarts and rolls back if the app does not come up. CI does the same after green tests on main (.github/workflows/deploy.yml).
set -euo pipefail
HOST="${1:?usage: push.sh deploy@host}"
cd "$(dirname "$0")/../.."
# Web app: the built namma-web repo (npm run build there first). Skipped when there is no build.
WEB_BUILD="${WEB_BUILD:-../namma-web/dist}"
if [ -d "$WEB_BUILD" ]; then
  tar -C "$WEB_BUILD" -czf - . | ssh "$HOST" sudo namma-deploy web
else
  echo "no web build at $WEB_BUILD (cd ../namma-web && npm ci && VITE_API_MODE=http npm run build); shipping the API only" >&2
fi
tar --exclude=.git --exclude=.venv --exclude=./var --exclude=.env --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=node_modules --exclude=./web -czf - . | ssh "$HOST" sudo namma-deploy api
