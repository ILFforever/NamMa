# CLAUDE.md — Flood Agent (working name: NamMa / น้ำมา)

Read this whole file before writing code. It is the shared context for every teammate's Claude Code session.

## 1. What we're building

A Thai-language **flood assistant** on **Huawei Cloud**, delivered through **LINE OA** (primary) and a **web app** (secondary). The user asks in plain Thai ("น้ำจะถึงบ้านแม่ที่บางระกำไหม"). The agent answers with a decision, using live government water data and what it remembers about the user's household. When the river near a saved location crosses a threshold, it pushes a LINE alert. If the user says they're in danger, the agent gives DDPM 1784 first and tells them what to say when they call (place, who needs help, how many people). **NamMa files no rescue request on anyone's behalf** (decision 2026-10-19: rescue tickets and the responder dashboard were removed). A flood tab on the web shows GISTDA's satellite flood extent.

**Pitch:** Thailand already has the data (ThaiWater, GISTDA, DDPM Cell Broadcast). It's fragmented across 6+ tools, and none of them answers "what should *my family* do *now*". Google Flood Hub knows the river; we know the household.

**Context:** As of mid-Oct 2026, floods affect 2–3M people across ~20 provinces + Bangkok. This is live, and judges are living through it.

**Deadline:** 7 days from 2026-10-14 → demo ~2026-10-21. Scope ruthlessly. Demo one basin: **Chao Phraya corridor (Nakhon Sawan → Chai Nat → Ayutthaya → Bangkok)**.

## 2. Non-negotiable product rules

1. **Numbers come only from data/tools, never from the LLM.** The LLM writes the words; tools supply every water level, trend, distance and time estimate.
2. **Every answer about risk names its source** ("สถานีวัดน้ำ C.35 บ้านป้อม ห่าง 3 กม.").
3. **We are unofficial.** Every alert and every risk answer points to DDPM: hotline **1784**, LINE **@1784DDPM**, and official Cell Broadcast warnings. Never tell someone they are safe in a way that discourages following official evacuation orders.
4. **Station ≠ house.** Station data is a river point. Phrase house-level estimates as estimates, combined with GISTDA flood extent and recurrence for the actual point.
5. **Memory stores only what the user stated.** Never infer health conditions. Health or vulnerability facts need explicit PDPA consent first (see §7).

## 3. Architecture

**Decision (2026-10-14): run our code on one ECS VM, not FunctionGraph + APIG.** One always-warm FastAPI app serves the LINE webhook (no cold starts; LINE expects a fast reply), the web API and the web app, and runs ingest/alert on a schedule. It's fewer Huawei services to learn in 7 days. The code is plain Python with no ECS-specific parts, so we can move to FunctionGraph later if it scores points.

```
          LINE OA ──webhook──┐                ┌── Web app (served by the same app)
                             ▼                ▼      chat · station map · satellite flood map
                      ECS VM: FastAPI app ◄───┘      login via LINE Login or Google
                       │   ├─ MaaS LLM (Qwen / DeepSeek / GLM): chat, memory extraction
                       │   ├─ tools → RDS (stations, readings, users, memory, flood cells)
                       │   └─ tools → GISTDA API (flood recurrence at a point)
                       │
                       ├─ scheduler, every 10 min: ingest ThaiWater → RDS (+ raw JSON to OBS)
                       ├─ alert job after each ingest → compute risk → LINE push (deduped, with cooldown)
                       └─ every 3 h: GISTDA 3-day + 7-day flood layers → RDS (only when they changed)
   Forecast model: LightGBM trained on a laptop (CPU is enough), scores written to RDS.
                   ModelArts only if we want it on the slide.
```

| AWS mental model | Huawei service we use |
|---|---|
| EC2 | ECS (our app) |
| RDS Postgres | RDS for PostgreSQL (local Docker Postgres until ~day 3 to save credits) |
| S3 | OBS |
| Bedrock | MaaS |
| SageMaker | ModelArts (optional) |
| Lambda / API Gateway | FunctionGraph / APIG (not used, see decision above) |

**Region:** AP-Bangkok. Confirm the exact region ID in the console before hardcoding it; it lives in `.env` as `HW_REGION`.

### Budget: $200 Huawei credit
Estimated total is ~$35–80. Rules:
- **Pay-per-use only**, never monthly/yearly. EIP billed **by traffic**.
- **No GPU instances**, ever. LightGBM is CPU.
- Use a standard chat model for chat, not a reasoning model (R1/QwQ thinking tokens cost 5–10×).
- Set Billing Center budget alerts at $50 and $100.
- Stop or delete resources after the demo.
- Check whether the credit covers all services or only MaaS.

## 4. Data sources (verified 2026-10-14)

All ThaiWater endpoints are public, keyless JSON used by thaiwater.net itself. They are **not officially documented**, so cache responses, poll at most every 10 min, wrap them in one client module (`ingest/thaiwater.py`), and credit HII (สถาบันสารสนเทศทรัพยากรน้ำ) in the UI.

**Gotchas (verified):**
- Numeric fields in `waterlevel_load` are **strings** (`"4.65"`). `waterlevel_graph` values are floats.
- **Station ids are shared between endpoints**: `station.id` from 4.1 is the `station_id` for 4.2.
- **Cadence differs by agency:** HII stations (`CPY0xx`) report every 10 min with no discharge. RID key stations (`C.35`, `C.2`…) report **hourly** and include `discharge` (m³/s). Use RID stations as the primary demo sources.
- `waterlevel_datetime` is local Thai time (UTC+7), no timezone suffix.

### 4.1 ThaiWater: current water levels (all stations)
`GET https://api-v3.thaiwater.net/api/v1/thaiwater30/public/waterlevel_load` (~1.4 MB, ~800 stations, <1 s)

Response: `waterlevel_data.data[]`. Useful fields per record:
- `waterlevel_datetime`, `waterlevel_msl`, `waterlevel_msl_previous`, `discharge` (RID only)
- `situation_level` (1–5; 5 = overflow; **missing on some records**), `storage_percent`
- `diff_wl_bank` (m, always positive) + `diff_wl_bank_text` ("ล้นตลิ่ง (ม.)" = above bank, "ต่ำกว่าตลิ่ง (ม.)" = below). Apply the sign from the text.
- `station.id`, `station.tele_station_oldcode` (e.g. `C.35`, `CPY011`), `station.tele_station_name.{th,en}`, `station.tele_station_lat/long`, `left_bank`, `right_bank`, `min_bank`, `ground_level`, `is_key_station`, `critical_level_msl`
- `geocode.{province_name, amphoe_name, tumbon_name}.{th,en}`, `basin.basin_name`, `river_name`, `agency.agency_shortname.en` (HII, RID, EGAT…)

### 4.2 ThaiWater: water level history (per station)
`GET https://api-v3.thaiwater.net/api/v1/thaiwater30/public/waterlevel_graph?station_type=tele_waterlevel&station_id={id}&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`

Response: `data.graph_data[]` with `{datetime, value, value_out, discharge}` plus `data.{min_bank, ground_level, warning_level, critical_level, qmax}`. Contains null gaps.
- ✅ **Same datum as `waterlevel_msl`**: CPY011 4.65 vs 4.648, C.35 4.90 vs 4.90 at the same timestamp.
- ✅ **Max ~366 days per request.** Longer ranges are **silently truncated to the last 366 days**. Backfill year by year (`ingest/backfill.py`).
- ✅ **Depth:** CPY011 (HII, Ayutthaya) has data back to 2012, patchy in some years (2017 empty, 2012/2014 sparse). C.35 (RID) has 2025 at ~80% hourly coverage, and nothing found for 2011–2017. Per-station, per-year coverage for 2018–2025: run `python -m ingest.backfill --coverage`.

### 4.3 ThaiWater: rainfall
Latest: `GET .../public/rain_24h` (~4.7 MB, ~4,600 stations). `data[]` with `rain_24h`, `rain_1h`, `rainfall_datetime`, `station.{id, tele_station_name, tele_station_lat, tele_station_long, tele_station_oldcode}`, `geocode`, `basin`, `agency`. Rain station ids are a **different id space** from water level stations.

History: `GET .../public/rain_24h_graph?station_type=rainfall_24h&station_id={rain_station_id}` returns hourly `{rainfall_datetime, rainfall_value}` but **ignores start/end dates and always returns the last ~40 h**. There is no rainfall history. Our own logging is the history. Start ingesting rain on day 0.

### 4.4 GISTDA (needs a free API key from https://api-gateway.gistda.or.th)
The key goes in the **`API-Key` header**, never the URL (responses' `links` echo the request URL). Without a key the
gateway answers 407; with a bad one 401 `INVALID_API_KEY` (checked 2026-10-19, any path).
- ~~`gi-service/v1.0/disasters/flood-extent-1day?lat&lon`~~ (point query): **dead**. Replaced by the layers below.
- **Flood extent layers** (satellite), OGC API Features, one GeoJSON feature per flooded **H3 res-9 cell** (~0.1 km²):
  `GET https://api-gateway.gistda.or.th/api/2.0/resources/features/flood/{1day|3days|7days|30days}?limit=&offset=`
  (`bbox` and `pv_idn` filter; `skipGeometry`/`properties` are ignored, so geometry always comes back, ~3 KB a cell).
  Reported sizes: 3 days ~80k cells, 7 days ~110k (a few hundred MB). 1-day/3-day are sometimes empty.
  Client + import: `ingest/gistda_flood.py` (3-day, 7-day + 30-day, every 3 h in the scheduler, skips an unchanged
  layer). All cells of a layer share its scenes (`file_name`, ~36 for 30 days): there is no date per cell, so
  "how lately" comes from the layers nesting (api/flood.py `seen`). 30 days ≈ 190k cells (~544 MB to parse).
  Map tiles also exist (`maps/flood/{period}/tms/{z}/{x}/{y}`, key needed, so they'd need a proxy): not used.
- Flood recurrence 2011–2023 at a point ("has this spot flooded before, how often"):
  `https://api-gateway.gistda.or.th/api/2.0/resources/gi-service/v1.0/disasters/flood-recurrence?lat={lat}&lon={lon}`
  Reported live (Oct 2026): `[{subdistrict_name, district_name, province_name, total, detail: [{year, freq}]}]` or
  `{"result": "not found"}`. Client: `agent/gistda.py`.

**Not yet checked against a response of ours** (no key yet): the field names come from other projects' write-ups.
The parsers look for each field under the reported names. Every page of each stored flood layer is kept (key removed)
under `var/raw/gistda/flood/<period>/<time>/` for `GISTDA_SNAPSHOT_KEEP_DAYS` (90), and its cells for good in
`flood_cells_history`, one copy per period per day. Check the parser against them once a key is in `.env`.

### 4.5 No API (handle manually)
- ~~Shelters: hand-curate a CSV~~ → now from DDPM, see §4.7.
- **DDPM alerts/bulletins:** no API. Don't scrape Facebook. Link to official channels instead.
- **BMA road sensors / Traffy Fondue:** stretch goals only, unverified.

### 4.7 DDPM shelters (verified 2026-10-18)
`GET https://shelter.disaster.go.th/api/v1/arcgis/shelter-layer/FeatureServer/0/query` (ArcGIS, public map layer of DDPM's shelter system, no key). ~20,000 shelters, ~12,000 public, all 77 provinces incl. Bangkok, live `is_active` (open now), `current_residents`, `capacity`, `qualification_accept_bedridden/elderly/disabled/...` (1 / 0 / **null on most rows** = not stated). No pets field. Max 2,000 rows per request (`resultOffset`). No licence stated: fetch once a day, credit ปภ. Coordinator names/emails/phones are personal data: never fetch. Client + import: `ingest/shelters.py` (daily, in the scheduler). The open CSV on catalog.disaster.go.th (`dpm-gd002`, 2024) has no Bangkok, Bang Ban, open status or bedridden field: not used.

### 4.8 Place names → coordinates (`data/gazetteer.csv`)
Every province, district and subdistrict (77 / 928 / 7,425) with official Thai names and polygon centroids, from the
Royal Thai Survey Department's admin boundaries on HDX (`cod-ab-tha`, v01 2022-01-22, **CC BY-IGO: credit RTSD / OCHA**),
plus the few names ThaiWater station geocodes spell differently. Rebuild: `python -m ingest.gazetteer`. Centroids are
area centres, not addresses: answers say "ตำแหน่งโดยประมาณจากชื่อพื้นที่".

### 4.6 Demo corridor stations (from live data, 2026-10-14)
Main stem, upstream → downstream. Ids are ThaiWater `station.id`.

| id | code | name | province | agency |
|---|---|---|---|---|
| 2795 | C.2 | ค่ายจิรประวัติ | Nakhon Sawan | RID |
| 2744 | C.13 | ท้ายเขื่อนเจ้าพระยา (below Chao Phraya Dam) | Chai Nat | RID |
| 2723 | C.3 | บ้านบางพุทรา | Sing Buri | RID |
| 2626 | C.7A | บ้านบางแก้ว | Ang Thong | RID |
| 2609 | C.35 | บ้านป้อม | Ayutthaya | RID |
| 2599 | C.12 | กรมชลประทานสามเสน | Bangkok | RID |

Tributaries/branches: 2611 C.36 (Noi River, Bang Ban), 2608 C.37 (Bang Ban canal), 2624 S.26 + 2607 S.5 (Pa Sak, joins at Ayutthaya). HII 10-min stations CPY001–CPY015 sit along the same stem. Full list: `data/corridor_stations.csv`.

## 5. Database (RDS for PostgreSQL)

Plain SQL migrations in `/infra/migrations`, applied in filename order by `python -m common.migrate`. No PostGIS; with ~1,000 stations, haversine is fine.

```
stations(id PK, oldcode, name_th, name_en, lat, lon, basin, river, province, amphoe, tambon,
         left_bank, right_bank, min_bank, ground_level, critical_level_msl, agency, is_key_station)
station_edges(upstream_id, downstream_id, approx_travel_hours)   -- hand-built for demo basin
waterlevel_readings(station_id, ts, level_msl, diff_wl_bank, situation_level, discharge,
                    PRIMARY KEY(station_id, ts))
rain_stations(id PK, oldcode, name_th, lat, lon, province, basin)   -- rain ids are a separate id space
rain_readings(station_id, ts, rain_1h, rain_24h, PRIMARY KEY(station_id, ts))
station_risk(station_id PK, computed_at, level, trend_cm_per_hr, hours_to_bank, risk_tier, forecast_json)

users(id PK, line_user_id UNIQUE, display_name, language DEFAULT 'th', created_at)
user_locations(id PK, user_id, label, lat, lon, notes)          -- "บ้าน", "บ้านแม่ บางระกำ"
user_memory(user_id PK, household JSONB, house JSONB, assets JSONB, needs JSONB,
            history JSONB, preferences JSONB, updated_at)
consents(user_id, kind, granted BOOLEAN, ts)                     -- kind: 'sensitive_memory', 'alerts'
messages(id PK, user_id, channel, role, content, ts)            -- keep last N per user
conversation_summary(user_id PK, summary, updated_at)

alerts_sent(id PK, user_id, location_id, station_id, risk_tier, ts)  -- dedupe + cooldown
shelters(id PK, name, lat, lon, capacity, accepts_patients, accepts_pets, phone)
flood_cells(period, h3, h3_r5..h3_r8, lat, lon, area_km2, pv/ap/tb_code, province, amphoe, tambon,
            observed_at, props JSONB, PRIMARY KEY(period, h3))       -- GISTDA layer, replaced per fetch
flood_fetches(id PK, period, fetched_at, cells, area_km2, observed_from, observed_to, fingerprint, by_province)
```
(`rescue_tickets`, `ticket_events` and `users.role` were dropped in migration 006.)

`diff_wl_bank` is stored **signed**: positive = above bank (overflow), negative = below.

## 6. Agent design

**Prefer function calling** if MaaS models support it through the API. **Check this on day 1.** If not, implement the same flow manually: classify intent → call tools in code → pass results to the LLM to write the answer.

Tools (each is a plain Python function with typed input and output; unit-test them all):
- `get_nearest_stations(lat, lon, k=3)` → level, diff to bank, situation level, 3h/12h trend, upstream status
- `get_forecast(station_id)` → from `station_risk` (rules, or model output if trained)
- `check_flood_extent(conn, lat, lon)` → the stored GISTDA layer (§4.4); `get_flood_recurrence(lat, lon)` → GISTDA
- `find_shelters(lat, lon, needs)` → filter by accepts_patients / accepts_pets
- `get_user_profile(user_id)` / `save_location(...)`

**Understanding (2026-10-19): LLM first, rules as guide and fallback.** Each typed message (not buttons or "ลบ …")
goes whole to one LLM call (`agent/understand.py`) with recent turns, saved places and the rules' guess as a hint. It
returns intent, place names (Thai) and stated household facts; that call replaces the memory-extraction call. Code
then resolves the names in the gazetteer (`geo.resolve`, never LLM coordinates) and runs the tools. The rules still
raise danger on their own (the LLM can add danger, never remove it), keep deletes on explicit commands, and decide
everything when the LLM is off or its output is not the JSON asked for. An `other` answer from the LLM is shown only
if it has no numbers but hotlines and never says "safe".

**Prompt assembly per turn:** system prompt (rules from §2 + tone: calm, direct, Thai) → user memory block (only consented fields) → conversation summary + last ~10 messages → tool results → user message.

**Danger:** if the user signals danger ("น้ำเข้าบ้านแล้ว", "ติดอยู่", "ช่วยด้วย"), the reply leads with the DDPM 1784 contact, says NamMa does not send for help on their behalf, lists what to tell 1784 (the place in words or GPS coordinates, who needs help, how many people, from the message and memory) and what to do while waiting. No ticket, no confirm step (`agent/triage.py`).

## 7. User memory

What to remember (structured fields in `user_memory`, not free text):
- **Places** → `user_locations`
- **Household:** elderly, bedridden, children, pregnant, pets. ⚠ sensitive
- **House:** stories, raised ground floor, near canal
- **Assets:** car, motorbike, ground-floor shop
- **Needs:** medication, dialysis, oxygen or other power-dependent devices. ⚠ sensitive
- **History:** past flooding at this place ("2011 น้ำท่วม 1 เมตร")
- **Preferences:** language, alert hours, verbosity

**Extraction:** after each user message, a second LLM call returns JSON of *newly stated durable facts* or `{}`. Store only stated facts; never infer.

**PDPA:** health and vulnerability details are sensitive personal data under Thailand's PDPA. Before saving any ⚠ field, send a one-tap LINE consent prompt ("ขออนุญาตจำข้อมูลนี้ เพื่อแจ้งเตือนและช่วยเหลือได้เร็วขึ้นไหม"). Record it in `consents`. Without consent, use the fact for this conversation only.

**User control:** "ข้อมูลอะไรที่จำเกี่ยวกับฉัน" shows the profile; "ลบ ___" deletes it. Implement both as quick replies.

## 8. Risk and alert logic

Rules first; the ML forecast is an add-on.

- Per station: `diff_wl_bank`, `situation_level`, trend (cm/hr over the last 3h from our readings), `hours_to_bank` = remaining gap / trend when rising.
- Upstream check: upstream stations (via `station_edges`) already overflowing → raise downstream risk.
- Risk tiers: `normal` → `watch` (rising and < 0.5 m to bank) → `warning` (< 6h to bank, or upstream overflow) → `danger` (over bank).
- Alerts: for each user location, take the nearest relevant station(s). Push only when the tier **increases**, or after a 6h cooldown at `danger`. Respect quiet hours unless `danger`. Escalate tone for households with vulnerable members.
- Every push includes: what is happening (with numbers and source), what to do now (personalized), nearest shelter, DDPM 1784.

## 9. Forecast model (stretch goal, gated on data)

**Go/no-go (day 2):** history endpoint returns ≥ 2–3 years for the demo-basin stations → GO. Otherwise ship rules only and call ML "future work". **Decision (2026-10-17): GO.** 2018–2026 backfilled for the corridor. `python -m model.forecast train` beats persistence at every horizon for all 7 modelled stations on Sept–Oct 2026 held out (C.35 +12 h: 5.5 cm vs 14.3 cm). Report: `model/artifacts/report.json`. Rule backtest (`model.backtest`): warning 4–27 h before each over-bank onset.

- Target: station level at +6h / +12h / +24h / +48h.
- Features: own lagged levels and trend, upstream levels with lags based on `station_edges` travel times, upstream rain sums (**only from our own logging; no rain history exists, see 4.3**, so likely drop rain from training), discharge below the Chao Phraya Dam (C.13).
- Model: LightGBM, trained on a laptop. ModelArts optional.
- Validation: train on prior years; **test on Sept–Oct 2026, held out**. It must beat a persistence baseline (level stays the same). Report MAE per horizon. This chart is a pitch slide.

## 10. Repo layout

```
/common     config (.env), DB connection, migration runner
/ingest     ThaiWater client, ingest job, history backfill
/agent      tools, risk rules, chat flow, memory extraction, triage, alerts, jobs (scheduler)
/api        FastAPI app: LINE webhook, web API (stations, shelters, flood, chat, profile, auth), demo endpoints,
            serves the built web app
/line       LINE webhook adapter + push sender (Messaging API)
/model      travel times, LightGBM forecast (train/score), rule backtest; artifacts/ not in git
/infra      SQL migrations, Huawei console checklist, deploy scripts
/data       Curated CSVs: corridor stations, station_edges, shelters, demo rescue messages (Thai)
/tests      pytest; fixtures are trimmed real API responses
```

Python 3.12. Local dev: `docker compose up -d` (Postgres) → `python -m common.migrate` → `python -m ingest.run`.

**Two repos (split 2026-10-17):** this one (**`NamMa-CEDT/namma-backend`**) and **`NamMa-CEDT/namma-web`** (the web app, history kept). Clone them side by side: `../namma-web` is the default for the API's `WEB_DIST` (`../namma-web/dist`), `ingest.export_web_fixtures` and `infra/deploy/push.sh`. A contract change is a PR in each repo, linked, changing `docs/api.md` here and `src/api/types.ts` + the mock there together.

**Web (`namma-web` repo, built 2026-10-16):**
- **Stack:** Vite + React 19 + TS + Tailwind 4, react-router (`HashRouter`), TanStack Query, react-leaflet, Recharts.
- **Run:** `cd ../namma-web && npm ci && npm run dev`, then open http://localhost:5173. Checks: `npm run typecheck && npm test && npm run build`.
- **API contract:** `docs/api.md` (here) + `src/api/types.ts` (namma-web).
- **Mock mode (default, `VITE_API_MODE=mock`):** an in-app mock backend (`src/api/mock/`) serves real ThaiWater fixtures. Refresh them with `python -m ingest.export_web_fixtures`. The flood tab's cells in mock mode are a **labelled sample**, not GISTDA data. Mock state lives in localStorage, so tabs share it. `?demo=1` opens a demo panel: demo login, simulate a tier, reset.
- **Real backend:** `VITE_API_MODE=http` proxies `/api` to `:8000`. Mock code is excluded from that build.
- **Why `HashRouter`, no service workers:** OBS can't rewrite deep links, and the LIFF browser has no service workers.
- **Thai text** lives in `src/copy.ts` (namma-web).

## 11. Huawei Cloud: how to work with it

Claude knows AWS far better than Huawei Cloud and **will** guess wrong parameter names. Rules:
1. Before writing infrastructure or SDK code for a service, **fetch and read its docs**. Don't rely on memory.
2. Prove each service with a tiny script (connect, one call, print result) before building on it.
3. **Decision (2026-10-17): no CLI.** All Huawei setup goes through the web console (a teammate, or Claude in Chrome on their machine). Checklist: `infra/huawei/README.md`. The app itself calls no Huawei API with account keys, so no AK/SK on the VM.

References (verify links before relying on them):
- Huawei Cloud docs portal: https://support.huaweicloud.com/intl/en-us/
- Terraform provider: https://registry.terraform.io/providers/huaweicloud/huaweicloud/latest/docs
- Python SDK: https://github.com/huaweicloud/huaweicloud-sdk-python-v3
- LINE Messaging API: https://developers.line.biz/en/docs/messaging-api/
- LINE Login: https://developers.line.biz/en/docs/line-login/

## 12. Secrets and safety

- Do console work as an IAM sub-user with only the needed services, not the main account. LINE channel secret/token, GISTDA key and MaaS key go in env vars or `.env` (gitignored). **Never commit secrets.** See `.env.example`.
- **Ask a human before** any command that creates, deletes, resizes or reconfigures cloud resources, or changes IAM. Creating resources spends the $200 credit.
- Rate-limit outbound calls to ThaiWater and GISTDA. Cache aggressively.

## 13. Day plan

| Day | Goal | Done when |
|---|---|---|
| 0 (tonight) | Accounts, IAM, region, MaaS enabled, repo, `.env.example`. **Ingest job live.** | Readings landing in Postgres every 10 min (local is fine) |
| 1 | Data spikes: GISTDA key + one response each, MaaS function-calling test. Build `station_edges` travel times for the demo basin. | All §14 open questions answered |
| 2 | Backfill history. Risk rules + `station_risk`. Tools with unit tests. Go/no-go on ML. | `get_nearest_stations` returns correct live answers |
| 3 | ECS + RDS up. Agent API + LINE webhook: chat works end to end in Thai, location sharing, saved locations | Ask about a real place in LINE, get a sourced answer |
| 4 | Memory extraction + PDPA consent + profile view/delete. Alert job + LINE push with dedupe. | Saved location triggers a push in a test |
| 5 | Rescue triage + responder dashboard. Web app chat + map with LINE Login. (ML training in parallel if GO.) | Danger message → ticket appears on dashboard |
| 6 | Backtest on Sept–Oct 2026 (forecast or rules). Polish Thai copy. Record demo video. | Backtest chart + video done |
| 7 | Slides, buffer, rehearse | — |

## 14. Open questions (resolve day 1, then update this file)

- [ ] Huawei Bangkok region ID; MaaS availability and model list there; function-calling support
- [x] `waterlevel_graph` datum: same as `waterlevel_msl` (§4.2)
- [x] `waterlevel_graph` history depth: ≤366 days per call; corridor backfilled 2018–2026 (RID from ~2020, HII from 2018–19).
- [x] Rainfall history endpoint: none (§4.3). Log our own from day 0.
- [ ] GISTDA response shapes: flood layers and recurrence. Endpoints and auth known (§4.4); field names still from other projects' reports
- [x] ~~FunctionGraph runtime / cold start~~: moot, we run on ECS (§3)
- [ ] LINE OA plan push-message quota (Thailand)
- [ ] Does the $200 credit cover all services or only MaaS?
- [x] Demo script §15 Bang Rakam: works. Answers use the nearest of all ~800 live stations (Y.64/Y.16 on the Yom), and `get_nearest_stations` fetches 2 days of history on demand so the trend is real. Only upstream edges and the ML forecast are corridor-only.
- [ ] **Web hosting.** In AP-Bangkok, OBS's default domain adds `Content-Disposition: attachment`, so pages download instead of rendering. We need a custom domain + HTTPS (LIFF and geolocation need HTTPS too). Recommendation: serve namma-web's `dist` from the ECS FastAPI app on the same origin as `/api` (one cert, no CORS); keep OBS+CDN as a later upgrade.
- [ ] **Web login.** Recommendation: LIFF on a LINE Login channel under the **same provider** as the OA channel, so the userId matches. The backend verifies the ID token. `initSession()` in namma-web `src/auth/session.ts` is the hook point. Publish the channel or add judges as Testers before the demo.
- [x] ~~Responder access + audit~~: moot, rescue tickets and the responder role were removed (2026-10-19).

## 15. Demo script (build toward this)

1. LINE: "บ้านแม่อยู่บางระกำ พิษณุโลก น้ำจะถึงไหม" → sourced answer with station, gap to bank, trend, recurrence, and what to do.
2. Agent asks about the household; user says grandma is bedridden → consent prompt → saved.
3. Simulated tier jump at the nearest station → personalized push: earlier evacuation, a shelter that accepts patients, 1784.
4. "น้ำเข้าบ้านแล้ว ยายเดินไม่ได้" → 1784 first, and what to tell them: บ้านแม่'s district, ยาย who can't walk.
5. Web flood tab: GISTDA's satellite flood cells on a satellite map, flooded area per province and district.
6. Backtest slide: our warning lead time on the Sept 2026 event.
