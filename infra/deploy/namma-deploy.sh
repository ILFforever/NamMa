#!/usr/bin/env bash
# Server side of every deploy. harden.sh installs it as /usr/local/bin/namma-deploy; the `deploy` user may run
# only this command as root (sudoers), so a leaked CI key can ship code but not get a root shell.
#
#   tar -czf - .           | ssh deploy@host sudo namma-deploy api   # backend → /opt/namma, install, migrate, seed, restart
#   tar -C dist -czf - .   | ssh deploy@host sudo namma-deploy web   # web build → /opt/namma-web/dist
#
# api: if the app does not come back, the previous code is restored and restarted. Migrations are not undone.
set -euo pipefail
APP=/opt/namma
WEB=/opt/namma-web/dist
KEEP=/var/lib/namma-deploy     # previous release, for rollback
mkdir -p "$KEEP"

new=$(mktemp -d)
trap 'rm -rf "$new"' EXIT
tar -xzf - -C "$new"           # unpack first: a broken upload must not touch the live code

replace_code() {               # $1 = dir with the new code; keeps .env, .venv and var
  # Trained models are gitignored, so a CI upload has none: keep the VM's unless the upload brings new ones.
  models=$(mktemp -d)
  cp -a "$APP"/model/artifacts/*.txt "$models"/ 2>/dev/null || true
  find "$APP" -mindepth 1 -maxdepth 1 ! -name .env ! -name .venv ! -name var -exec rm -rf {} +
  cp -a "$1"/. "$APP"/
  mkdir -p "$APP/model/artifacts"
  cp -an "$models"/. "$APP/model/artifacts"/ && rm -rf "$models"
  chown -R namma:namma "$APP"
}

install_and_restart() {
  sudo -u namma bash -c 'cd /opt/namma && .venv/bin/pip install -q -r requirements.txt &&
    .venv/bin/python -m common.migrate && .venv/bin/python -m common.seed' && systemctl restart namma
}

app_up() {                     # /healthz answers 200, or 503 when risk is stale: either way the app and DB are up
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz || true)
    [[ $code == 200 || $code == 503 ]] && return 0
    sleep 2
  done
  return 1
}

case "${1:-}" in
  api)
    test -f "$new/requirements.txt" || { echo "upload has no requirements.txt, not a backend tree" >&2; exit 2; }
    tar -C "$APP" --exclude=./.env --exclude=./.venv --exclude=./var -czf "$KEEP/prev.tgz" .
    replace_code "$new"
    if install_and_restart && app_up; then
      echo "deploy ok: $(curl -s http://127.0.0.1:8000/healthz)"
    else
      echo "deploy FAILED, restoring previous code" >&2
      prev=$(mktemp -d); tar -xzf "$KEEP/prev.tgz" -C "$prev"; replace_code "$prev"; rm -rf "$prev"
      install_and_restart || true
      journalctl -u namma -n 30 --no-pager >&2 || true
      exit 1
    fi
    ;;
  web)
    test -f "$new/index.html" || { echo "upload has no index.html, not a web build" >&2; exit 2; }
    mkdir -p "$(dirname "$WEB")"
    rm -rf "$WEB.prev"
    [ -d "$WEB" ] && mv "$WEB" "$WEB.prev"
    mv "$new" "$WEB" && mkdir "$new"          # keep the trap's rm -rf harmless
    chmod 755 "$WEB" && chown -R namma:namma "$(dirname "$WEB")"
    echo "web ok"
    ;;
  *)
    echo "usage: namma-deploy api|web  (tar.gz on stdin)" >&2; exit 2 ;;
esac
