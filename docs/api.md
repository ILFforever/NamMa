# NamMa web API contract (v0, 2026-10-16)

The FastAPI app on ECS serves this API to the web app (the **`namma-web`** repo). Paths below starting `src/` are in that repo. The web app can run against an in-app mock of the API (`src/api/mock/`), so **the mock is the executable spec**. Its tests in `src/api/mock/mock.test.ts` pin the behaviour below. TypeScript types: `src/api/types.ts`. Keep this file, the types and the mock in sync. If the backend needs a different shape, change all three together: one PR in each repo, linked to each other.

Switch the web app to the real backend with `VITE_API_MODE=http` (the dev server proxies `/api` to `http://localhost:8000`).

## Conventions

- **Success:** JSON envelope `{ "data": <payload>, "meta": { "ingested_at": "<ts>" | null } }`.
  - `meta.ingested_at` is when ThaiWater was last ingested.
  - The UI shows a "data may be stale" banner when it is older than 30 min.
- **Errors:** `{ "error": { "code": "<snake_case>", "message": "<english>" } }` with a 4xx/5xx status. Codes used:
  - `bad_request` (400)
  - `unauthorized` (401)
  - `invalid_token` (401)
  - `forbidden` (403)
  - `not_found` (404)
  - `conflict` (409)
- **Timestamps:** ISO-8601 **with an offset**. Any offset works; the client always displays in `Asia/Bangkok`. Note that `timestamptz` serializes in the DB session's TimeZone, so set it or format explicitly.
- **Numbers come from data only** (CLAUDE.md §2). Distances are computed server-side. Unknown values are `null`, never a guess.
- **Auth:** `Authorization: Bearer <session_token>` from `POST /api/auth/line`.
- **Shared state:** web and LINE use the same `users` row (same LINE userId).

## Stations

### `GET /api/stations` → `StationSummary[]`
All stations the map shows (today: the demo corridor in `data/corridor_stations.csv`).

```jsonc
{
  "id": 2609, "oldcode": "C.35", "name_th": "บ้านป้อม", "name_en": "Ban Pom",
  "lat": 14.3691, "lon": 100.528732,
  "river": "แม่น้ำเจ้าพระยา", "province": "พระนครศรีอยุธยา", "amphoe": "พระนครศรีอยุธยา",
  "agency": "RID", "is_key_station": true,
  "stem_order": 10,            // from corridor_stations.csv; null off the main stem
  "role": "main",              // main | branch | tributary | null  (CSV column, not in DB yet)
  "latest": {                  // null if no reading
    "ts": "2026-10-16T13:00:00+07:00", "level_msl": 4.69,
    "diff_wl_bank": 0.34,      // SIGNED: + above bank, - below
    "situation_level": 5,      // 1–5 or null
    "discharge": 1254.0        // m³/s, RID only, else null
  },
  "risk": {                    // station_risk row; null if not computed. UI shows "ไม่ทราบ", never "ปกติ"
    "tier": "danger",          // normal | watch | warning | danger
    "trend_cm_per_hr": -0.67, "hours_to_bank": null,
    "computed_at": "2026-10-16T13:00:00+07:00"
  }
}
```

### `GET /api/stations/nearest?lat=&lon=&k=3` → `NearbyStation[]`
Same objects plus `distance_km` (haversine, 1 decimal), nearest first. `k` is 1–10. Returns 400 without lat/lon.

### `GET /api/stations/{id}` → `StationDetail`
The summary plus `left_bank`, `right_bank`, `min_bank`, `ground_level`, `critical_level_msl`, and:
- `upstream: [{ station: StationSummary, approx_travel_hours: number | null }]`
- `downstream: [...]`

Both come from `station_edges` and are empty for stations without edges (all HII stations today). Returns 404 if unknown.

### `GET /api/stations/{id}/readings?hours=72` → `{ station_id, readings: Reading[] }`
`Reading = { ts, level_msl, diff_wl_bank, discharge }`, oldest first, `hours` from 1 to 168. Hourly is enough for the chart. Gaps are fine: the client breaks the line where readings are missing.

## Shelters

### `GET /api/shelters?lat=&lon=&patients=true&pets=true&limit=50` → `Shelter[]`
- `{ id, name, lat, lon, capacity, accepts_patients, accepts_pets, phone, distance_km,
    is_open, current_residents, accepts_bedridden, province, district, source, updated_at }`
- Source: DDPM's live shelter system (`ingest/shelters.py`, daily, ~12,000 public shelters). Credit ปภ. in the UI.
- `is_open`: open for admission now (DDPM). `accepts_bedridden`: `true` / `false` / `null` = DDPM doesn't say (most).
  `accepts_patients` is `true` only when DDPM confirms. `accepts_pets` is always `false`: no source has it.
- `phone` is null: DDPM coordinator phones are personal data and not stored. Point people to 1784.
- `patients`/`pets` filter to shelters that accept them. `limit` 1–500 (default 50).
- With lat/lon the list is the nearest `limit`, sorted by distance; without them `distance_km` is null.

## Rescue tickets (responder role only)

401 without a session, 403 for non-responders. **Access control is the backend's job**: the static bundle is public, so hiding the route protects nothing.

### `GET /api/tickets?status=open,acknowledged&since=<ts>` → `TicketSummary[]`
The list view deliberately **omits `raw_text`, `contact_phone`, `assignee`, `notes`** (PDPA: show the minimum).
- `status` is comma-separated.
- `since` returns tickets with `updated_at > since`.
- The client polls every 5 s and sorts by urgency, then age.

```jsonc
{ "id": 1047, "place_label": "อ.บางบาล จ.พระนครศรีอยุธยา", "lat": 14.369, "lon": 100.474,
  "people_count": null, "vulnerable": ["elderly", "bedridden"], "needs": ["evacuation"],
  "urgency": "critical", "status": "open", "channel": "web",
  "created_at": "…", "updated_at": "…" }
```

### `GET /api/tickets/{id}` → `RescueTicket`
The summary plus `raw_text`, `contact_phone`, `assignee`, `notes`.

### `PATCH /api/tickets/{id}` → `RescueTicket`
Body: `{ status?, assignee?, note?, updated_at }`.
- `updated_at` is the value the client last saw. If the ticket changed since, answer **409 `conflict`**.
- `note` is appended to `notes`.
- Return the updated ticket.

### Enums (propose adding CHECK constraints in migration 002)
- **`urgency`:** `critical | high | normal`
- **`status`:** `open | acknowledged | in_progress | resolved | cancelled`
- **`vulnerable[]`:** `elderly | bedridden | child | infant | pregnant | disabled | patient | pet`
- **`needs[]`:** `evacuation | medical | medication | oxygen | dialysis | food_water | boat | power`

## Chat

### `POST /api/chat` → `ChatResponse`
Request: `{ session_id, message? | postback? | location?: { lat, lon } }`. Exactly one of the three.

```jsonc
{
  "reply": "ใกล้ อ.บางบาล … สถานีวัดน้ำที่ใกล้ที่สุดคือ C.37 …",   // LLM-written words, tool-supplied numbers
  "sources": [ { "type": "station", "station_id": 2608, "code": "C.37", "name_th": "บ้านบางบาล",
                 "distance_km": 1.3, "observed_at": "…" } ],         // or {type:"shelter",…} / {type:"gistda",…}
  "quick_replies": [ { "type": "message", "label": "มียายติดเตียง", "text": "ที่บ้านมียายติดเตียง" },
                     { "type": "postback", "label": "อนุญาต", "data": "consent:sensitive_memory:yes", "display_text": "อนุญาต" },
                     { "type": "location", "label": "ส่งตำแหน่ง" } ],
  "emergency": false,     // true → UI shows the DDPM 1784 card above the reply
  "ticket_id": null       // set when this turn created a rescue ticket
}
```

- **Station sources carry the reading** the web StationCard draws, so the card shows exactly the numbers the reply used:
  `{ type: "station", station_id, code, name_th, distance_km, observed_at, river, agency, amphoe, province,
  level_msl, diff_wl_bank, bank_msl, trend_cm_per_hr, hours_to_bank, risk_tier, upstream_over: string[] }`.
  `diff_wl_bank` is signed (+ over bank); `bank_msl` = `level_msl − diff_wl_bank`; `trend_cm_per_hr` is the 3 h trend;
  `upstream_over` lists upstream station codes already over bank. Any of the reading fields can be null.
- **Quick replies mirror LINE quick-reply actions** (`message` / `postback` / `location`). The same agent handler can serve the LINE webhook and the web.
- **Postback data the web sends today:**
  - `consent:sensitive_memory:yes|no` → write `consents`
  - `triage:confirm`
  - `triage:cancel`
  - `memory:delete_all`
- **Required behaviour** (see the mock's `chatScript.ts` and its test for the full demo flow):
  - Danger messages return `emergency: true`, with 1784 first (on the web, the EmergencyCard above the reply carries it).
  - Web replies are plain text without emoji and without the hotline lines the UI already draws: no rescue 1784/1669 line when `emergency` is true, no DDPM footer when `sources` has a station (HotlineNotice). LINE replies keep both.
  - A location shared within an hour of a rescue request replaces that ticket's coordinates (GPS beats the geocoded place) and sends it if it was unconfirmed.
  - Confirm before creating a ticket.
  - Ask for consent before saving vulnerable or health facts.
  - Say when a place is outside the demo basin instead of guessing.

## Profile

What NamMa remembers about the person chatting, for the web ProfilePanel (CLAUDE.md §7: view and delete).
Who: the signed-in user, else the anonymous chat from the **`X-Chat-Session: <session_id>`** header (the same
`session_id` as `POST /api/chat`). An unknown session gets the empty profile; nothing is created.

### `GET /api/profile` → `Profile`
```jsonc
{
  "places": [ { "id": 3, "label": "บ้านแม่", "lat": 14.37, "lon": 100.47, "approx": true,
                "where": "อ.บางบาล จ.พระนครศรีอยุธยา",
                "nearest": { "station_id": 2608, "code": "C.37", "name_th": "บ้านบางบาล", "distance_km": 1.3,
                             "risk_tier": "warning", "diff_wl_bank": -0.06, "observed_at": "…" } } ],
  "facts": [ { "kind": "household", "text": "ผู้ที่เดินไม่ได้/ติดเตียง: ยาย", "sensitive": true },
             { "kind": "house", "text": "บ้าน 2 ชั้น", "sensitive": false } ],
  "consent": { "granted": true, "ts": "…" },   // latest sensitive_memory decision; ts null = never asked
  "pending_consent": false,                     // a sensitive fact waits for consent (not shown, not saved)
  "line_linked": false                          // signed in with LINE
}
```
- `approx`: the place was geocoded from its name, not a GPS share. `nearest` is null when no fresh station is within 60 km.
- `facts[].kind` is one of `household`, `needs` (sensitive), `house`, `assets`, `history`.

### `DELETE /api/profile/places/{id}` → `Profile`
404 if the place isn't this person's.

### `DELETE /api/profile/facts/{kind}` → `Profile`
`kind`: a fact kind, or `all` (every fact and place, and any fact waiting for consent).

## Auth

### `POST /api/auth/line` → `{ session_token, user: { display_name, role, picture_url } }`
Body: `{ id_token }`.
- **Real mode:** verify a LIFF ID token with `POST https://api.line.me/oauth2/v2.1/verify` (`id_token`, `client_id` = LINE Login channel id). Use `sub` as the LINE userId.
- **Mock mode:** accepts `demo:user` / `demo:responder`.
- `role` is `user | responder`. Responders come from an allowlist or a `users.role` column.

### `GET /api/me` → `SessionUser`
Returns 401 without a session.

## Schema gaps for the backend owner

The contract needs these against `infra/migrations/001_init.sql`:

1. **`rescue_tickets`** needs `place_label`, `contact_phone`, `channel`, `assignee`, `notes`, plus CHECK constraints for `urgency` and `status`.
2. **`messages.user_id` is NOT NULL**, but the spec allows anonymous web chat. Make it nullable and add `session_id`, or require LINE login for web chat.
3. **No audit trail** for who viewed or changed a ticket. Suggested: `ticket_events(ticket_id, actor, action, ts)`. Tickets hold health data (PDPA sensitive).
4. **Responder role:** `users.role` or an allowlist.
5. **`stem_order` / `role`** exist only in `data/corridor_stations.csv`. Load them into `stations` or join the CSV.
6. **`station_edges`** has only RID stations, and `approx_travel_hours` is NULL until estimated.
