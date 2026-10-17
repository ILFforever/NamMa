# NamMa status — 2026-10-17 (evening)

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
| Agent | Rescue triage → ticket, pre-filled from memory, confirm step; a GPS share replaces the place-name location | DB test + live demo |
| Agent | Web replies: plain text, no duplicate hotline (the UI draws it); LINE unchanged | DB + API tests |
| Agent | Optional LLM advice, rejected if it adds numbers | Unit test |
| Alerts | Push only when tier rises, 6 h repeat at danger, quiet hours, personal tone | DB test; live alerts sent, no duplicates |
| API | Web API contract (`docs/api.md`): envelope + errors, LINE ID-token login + demo login, sessions, responder role, stations, shelters, tickets with audit trail, chat | 79 backend tests; end to end on live data |
| App | LINE webhook: signature check, reply, push, quick replies, crash fallback with 1784 | Unit test (not yet tested with real LINE) |
| Web | `namma-web`: chat, station map, responder dashboard on the NamMa design system; mock or real API | 26 tests, typecheck, build; end to end against the API |
| Deploy | ECS kit: service file, auto-HTTPS, setup + push scripts (ships the web build too), guide (`infra/deploy/`) | Written, not run (no VM yet) |

## To do

| Priority | What | Who | Blocked by |
|---|---|---|---|
| 🔴 | LINE channel → keys in `.env` | Owner | — |
| 🔴 | Create the ECS VM (`infra/huawei/README.md`), then run `infra/deploy/README.md` | Owner + dev | Credit approval |
| 🔴 | Point the LINE webhook at the VM; test the bot for real | Owner + dev | VM + LINE keys |
| 🔴 | Before the site is public: `DEMO_MODE=0`, no `VITE_DEMO_LOGIN`, set `RESPONDER_LINE_IDS` | Dev | VM |
| 🟠 | MaaS key; test Thai advice quality and function calling | Owner + dev | Key |
| 🟠 | GISTDA key; fix the parser against a real response | Owner + dev | Key |
| 🟠 | Replace sample shelters (`data/shelters.csv`) with real ones | Team | Real list |
| 🟠 | Rehearse demo step 3 (forced tier jump → alert) on LINE | Dev | LINE live |
| 🟡 | LINE Login (LIFF) on the web app | Dev | LINE Login channel |
| 🟡 | Charts for the backtest + model slides | Dev | — |
| 🟡 | Polish Thai wording (native-speaker review) | Team | — |
| 🟡 | OBS upload of raw archives | Dev | OBS bucket |
| 🟡 | Answer the open questions: region ID, LINE push quota, what the credit covers | Owner | Console access |
| ⚪ | Record the demo video, slides, rehearse | Team | Everything above |

## Key numbers for the pitch

| Metric | Value |
|---|---|
| Forecast error at C.35 Ayutthaya, +12 h | 5.5 cm (persistence: 14.3 cm) |
| Rules warning lead time, Sept 2026 event | 4–27 h before over-bank; watch 15–36 h before |
| Live coverage | ~800 water-level stations, ~4,700 rain stations |
