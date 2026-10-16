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

Data: water levels and rainfall from [ThaiWater](https://www.thaiwater.net) by HII (สถาบันสารสนเทศทรัพยากรน้ำ). NamMa is unofficial. For emergencies call DDPM **1784** or LINE **@1784DDPM**.
