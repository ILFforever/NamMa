# NamMa status — 2026-10-20

Two repos in the **NamMa-CEDT** org: `namma-backend` (this one) and `namma-web`. Work happens on branches + PRs.

## Done

| Area | What | How we know it works |
|---|---|---|
| Data | ThaiWater ingest every 10 min (water levels + rain) | Running; 803 stations |
| Data | History backfill 2018–2026, corridor stations | All 22 stations loaded |
| Data | Upstream travel times (`station_edges`) | Nakhon Sawan → Ayutthaya ≈ 54 h |
| Data | Place lookup from Thai text (3,333 areas) | "บางบาล อยุธยา", "บางระกำ พิษณุโลก" found |
| Risk | Risk rules (normal / watch / warning / danger) | Unit tests + live run |
| Risk | Trends for any station in Thailand (fetched on demand) | Bang Rakam shows a real trend |
| Risk | LightGBM forecast +6/12/24/48 h | Beats persistence at all 28 station × horizon checks (Sept–Oct 2026 held out) |
| Risk | Rules backtest | Warning 4–27 h before each over-bank event |
| Agent | Sourced Thai answer: station, distance, trend, forecast, advice, shelter, 1784 | Live demo |
| Agent | Saves "บ้านแม่" as a watched place | Live demo |
| Agent | Memory + PDPA consent; view and delete | DB test |
| Agent | Danger message: 1784 first, says NamMa sends nothing for them, lists what to tell 1784 (place in words or GPS coordinates, who needs help, how many) | DB + API tests |
| Agent | Chat flood check from the stored GISTDA layer (3-day, else 7-day): flooded at a GPS point / nearest flooded cell, or flooded area within 5 km of a named place | DB + API tests (made-up cells) |
| Agent | Web replies: plain text, no duplicate hotline (the UI draws it); LINE unchanged | DB + API tests |
| Agent | Optional LLM advice, rejected if it adds numbers | Unit test |
| Data | Real shelters from DDPM's live system, daily: ~12,000, open-now status, accepts bedridden (yes/no/not stated) | Live import; DB + unit tests |
| Alerts | Push only when tier rises, 6 h repeat at danger, quiet hours, personal tone | DB test; live alerts sent, no duplicates |
| API | Web API contract (`docs/api.md`): envelope + errors, LINE/Google login + demo login, sessions, stations, shelters, flood areas + cells, chat, profile | 108 backend tests; end to end on live data (flood: made-up cells) |
| Data | GISTDA 3-day + 7-day flood layers (`features/flood/…`, H3 cells): paged download every 3 h, skipped when unchanged, never stored partial; area totals history in `flood_fetches` | Unit + DB tests against a fake GISTDA; **not yet run against the real one (no key)** |
| Data | GISTDA flood history 2011–2024, all 77 provinces: our own copy (7.87M shapes → 769,808 H3 cells, years per tambon), raw pages on the VM disk | Every province's count matches GISTDA's `numberMatched`; read back by `/api/flood/tambon` on the VM (ต.บางบาล: 8 of 14 years) |
| Data | Our own record of GISTDA's live layers: every stored 3/7/30-day layer kept (raw pages 90 days on the VM disk, cells for good in `flood_cells_history`, one copy per period per day); the fetch runs as its own low-priority process | DB tests; real 30-day layer dry run on the VM: 187,778 cells, 13.3 min, peak 593 MB. **Not deployed yet** |
| Web | Flood map `30 วัน`: water seen this week vs only 8–30 days ago; tambon card tells its story (3/7/30-day area, years 2011–2024 + this year, our daily record); chat says "น้ำอาจลดลงแล้ว" when water was there this month but not now | 74 web + 175 backend tests, build, headless Chrome at 390 and 1280 px on the mock; tambon endpoint read-only on real VM data. **Not deployed yet** |
| Removed | Rescue tickets, the responder role and dashboard API (`/api/tickets`, `triage:*` postbacks, `demo:responder`); migration 006 drops the tables | Upgrade from 005 tested |
| App | LINE webhook: signature check, reply, push, quick replies, crash fallback with 1784 | Unit test (not yet tested with real LINE) |
| Web | `namma-web`: chat, station map, satellite flood tab (Leaflet + h3-js + Esri imagery, province → amphoe → tambon totals) on the NamMa design system; mock or real API | 40 tests, typecheck, build; headless Chrome at 320–1280 px, signed in and out |
| Deploy | ECS kit: service file, auto-HTTPS, setup + push scripts (ships the web build too), guide (`infra/deploy/`) | Written, not run (no VM yet) |

## To do

| Priority | What | Who | Blocked by |
|---|---|---|---|
| ✅ | LINE channel → keys in `.env` (bot NamMA, @753kmkri; token verified 2026-10-18) | Owner | — |
| 🔴 | Database for the first real LINE test (no Docker/Postgres on the owner's PC; suggested Neon), then cloudflared tunnel + webhook URL | Owner + dev | DB choice |
| 🟠 | LINE hardening: handle unfollow/push errors, reply to non-text events, dedupe redelivered events, fallback push when reply fails | Dev | — |
| 🔴 | Create the ECS VM (`infra/huawei/README.md`), then run `infra/deploy/README.md` | Owner + dev | Credit approval |
| 🔴 | Point the LINE webhook at the VM; test the bot for real | Owner + dev | VM + LINE keys |
| 🔴 | Before the site is public: `DEMO_MODE=0`, no `VITE_DEMO_LOGIN` | Dev | VM |
| 🟠 | MaaS key; test Thai advice quality and function calling | Owner + dev | Key |
| 🟠 | GISTDA key → `.env`; run `python -m ingest.gistda_flood` once and check the archived first page (`var/raw/gistda`) against the parser's field names, area unit (`flood_fetches.area_unit`) and recurrence shape | Owner + dev | Key |
| 🟡 | Phone check of the header and flood tab on a real device (done in headless Chrome only) | Team | — |
| 🟠 | Rehearse demo step 3 (forced tier jump → alert) on LINE | Dev | LINE live |
| 🟡 | LINE Login (LIFF) on the web app | Dev | LINE Login channel |
| 🟡 | Charts for the backtest + model slides | Dev | — |
| 🟡 | Polish Thai wording (native-speaker review) | Team | — |
| 🟠 | After deploying the GISTDA record: run the checks in `infra/deploy/README.md` (GISTDA record): site speed and memory during a 30-day fetch, then a week of daily copies | Dev | Deploy |
| 🟡 | OBS upload of raw archives (none possible for now: raw GISTDA pages stay on the VM disk, 179 GB free) | Dev | OBS bucket |
| 🟡 | Answer the open questions: region ID, LINE push quota, what the credit covers | Owner | Console access |
| ⚪ | Record the demo video, slides, rehearse | Team | Everything above |

## Key numbers for the pitch

| Metric | Value |
|---|---|
| Forecast error at C.35 Ayutthaya, +12 h | 5.5 cm (persistence: 14.3 cm) |
| Rules warning lead time, Sept 2026 event | 4–27 h before over-bank; watch 15–36 h before |
| Live coverage | ~800 water-level stations, ~4,700 rain stations |
