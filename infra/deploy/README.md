# Deploy to one Huawei ECS VM

One process does everything: FastAPI (LINE webhook, web API, the web app) plus the 10-minute
ingest → risk → alerts scheduler and, with `GISTDA_API_KEY` set, the 3-hourly GISTDA flood layers.
Caddy terminates HTTPS. Postgres runs on the same VM by default.

A GISTDA layer is a few hundred MB of download each time GISTDA publishes a new one (an unchanged layer costs one
page). That is inbound traffic; check that the EIP's by-traffic billing only counts outbound.

**Creating the VM spends the $200 credit. Ask the team first (CLAUDE.md §12).**

## 1. Console (one time)
- ECS: Ubuntu 24.04, smallest general-purpose size with 2 GB RAM, **pay-per-use**, AP-Bangkok.
- EIP: **billed by traffic**.
- Security group: inbound 22 from your IP only, 80 and 443 from anywhere. Never open 5432 or 8000.
- No domain? Use `<EIP>.sslip.io` (e.g. `1.2.3.4.sslip.io`). It resolves to the IP, and Let's Encrypt issues certificates for it.

## 2. Set up the VM
```bash
scp -r infra/deploy root@<EIP>:/root/deploy
ssh root@<EIP> 'NAMMA_DOMAIN=<EIP>.sslip.io bash /root/deploy/setup_ecs.sh'
```

## 3. Ship the code (and every later update)
```bash
scp .env root@<EIP>:/opt/namma/.env      # first time only, then edit it on the VM:
#   DATABASE_URL = the line from /root/namma_db_url.txt
#   SCHEDULER=1, GISTDA_API_KEY=<key>, DEMO_MODE=0 after the demo
(cd ../namma-web && npm ci && VITE_API_MODE=http npm run build)   # web app, from the namma-web repo
bash infra/deploy/push.sh root@<EIP>
```
`push.sh` uploads the web build to `/opt/namma-web/dist` (served on the same origin as `/api`; override the
local path with `WEB_BUILD=...`), then the repo (without `.env`, `.venv`, `var`), installs requirements,
runs migrations, does one ingest and the seed, then restarts the service.

After the first push, load history once (optional, slow):
`ssh root@<EIP> 'cd /opt/namma && sudo -u namma .venv/bin/python -m ingest.backfill --from 2026'`

## 4. Point LINE at it
LINE Developers console → Messaging API → Webhook URL: `https://<EIP>.sslip.io/line/webhook`,
turn on "Use webhook", turn off auto-reply messages. Click Verify.

## 5. Check
- `https://<EIP>.sslip.io/healthz` returns 200. It returns 503 if no risk refresh in 30 min, so point an uptime check at it.
- Logs: `journalctl -u namma -f`
- Web app: `https://<EIP>.sslip.io/#/chat`, station map `#/map`, satellite flood map `#/flood`
- GISTDA: `journalctl -u namma | grep gistda` shows each layer's cells and area, or why it was skipped.
  The first page of each new layer is kept under `var/raw/gistda` (key removed) to check the parser against.

## Moving to RDS later
Create RDS for PostgreSQL in the same VPC, set `DATABASE_URL` in `/opt/namma/.env`, run
`python -m common.migrate && python -m common.seed`, restart. Copy data with `pg_dump | psql` if needed.

## After the demo
Stop or delete the ECS VM and release the EIP (CLAUDE.md §3 budget).
