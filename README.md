# NamMa (น้ำมา)

A Thai flood assistant on LINE. It answers "will the water reach my family's house?" using live ThaiWater data and what it remembers about the household, pushes alerts when the nearest river rises, and turns "help" messages into rescue tickets.

**Read [CLAUDE.md](CLAUDE.md) first.** It holds the plan, rules, data findings and day plan.

## Local dev

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
docker compose up -d                  # Postgres on :5432
.venv/bin/python -m common.migrate    # apply infra/migrations
.venv/bin/python -m ingest.run        # one ThaiWater ingest (add --every 600 to loop)
.venv/bin/pytest -q
```

Web app (mock API with real ThaiWater fixtures; see `docs/api.md` for the contract):

```bash
cd web && npm install && npm run dev  # http://localhost:5173  (add #/map?demo=1 for the demo panel)
npm run typecheck && npm test && npm run build
python -m ingest.export_web_fixtures  # refresh web/src/api/mock/fixtures from live ThaiWater (repo root)
```

History: `python -m ingest.backfill --coverage` (read-only coverage table), then `python -m ingest.backfill --from 2018`.

## Run the app

```bash
.venv/bin/python -m common.seed                       # station_edges + shelters (after the first ingest)
SCHEDULER=1 .venv/bin/uvicorn api.main:app --port 8000  # API + LINE webhook + 10-min ingest → risk → forecast → alerts
```
- Chat + station map: http://localhost:8000 · Responder dashboard: `/web/dashboard.html?token=$RESPONDER_TOKEN`
- Demo step 3 (tier jump → alert): `POST /api/demo/simulate {"station_id": 2609, "tier": "danger"}`, undo with `POST /api/demo/refresh`
- Without MaaS / GISTDA / LINE keys everything still works: rule-based Thai, no satellite lines, web chat instead of LINE.

## Model and backtest

```bash
python -m model.travel_times --year 2025     # fills approx_travel_hours in data/station_edges.csv
python -m model.forecast train               # LightGBM +6/12/24/48 h; test = Sept–Oct 2026 held out
python -m model.backtest --from 2026-09-01   # rule-tier warning lead time before each over-bank onset
```

Deploy to Huawei ECS: [infra/deploy/README.md](infra/deploy/README.md).

No Docker? Any Postgres 16 on port 5433 with user/db `namma` works (e.g. the EDB Windows binaries zip, no admin needed).

Data: water levels and rainfall from [ThaiWater](https://www.thaiwater.net) by HII (สถาบันสารสนเทศทรัพยากรน้ำ). NamMa is unofficial. For emergencies call DDPM **1784** or LINE **@1784DDPM**.
