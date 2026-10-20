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

## Rivers

Every ThaiWater station with a river name, position and bank, grouped and ordered by `ingest.rivers` after each ingest
(design and evidence: `docs/rivers-api.md`, fixes: `data/river_overrides.csv`). For the home page's river charts.
A river lists each place once: where two stations gauge one place (< 3 km apart) the RID one (or the fresh one) stands
for it. Only rivers with ≥ 2 such stations.

### `GET /api/rivers?lat=&lon=&q=&limit=6` → `River[]`
- With `q`: rivers whose name contains `q`, most stations first; then rivers with a station whose province, amphoe,
  name or code contains `q` (or whose basin does), most stations first. `reason: "match"` for both.
- Otherwise: rivers with a station at watch or worse, worst tier first, then highest against its bank (≤ `limit`,
  `reason: "at_risk"`); then, given `lat`/`lon`, the river with the station nearest that point if not already listed
  (`"near_you"`). If that is nothing: the three best-gauged rivers (`"largest"`).
- 400 if only one of `lat`/`lon` is given.

```jsonc
{
  "key": "แม่น้ำเจ้าพระยา",          // stable; a second river of the same name is "<name>@<lowest station id>"
  "name_th": "แม่น้ำเจ้าพระยา",
  "basins": ["ลุ่มน้ำเจ้าพระยา"],
  "provinces": ["นครสวรรค์", "ชัยนาท", "…"],   // upstream first; tells same-named rivers apart
  "worst_tier": "danger",           // normal | watch | warning | danger | null (no station has a risk row)
  "reason": "at_risk",              // at_risk | near_you | largest | match
  "distance_km": 1.5,               // to its nearest station, when lat/lon given; else null
  "stations": [ /* StationSummary, upstream first */ ]
}
```

### `GET /api/rivers/{key}` → `River`
One river, all its stations (`reason: "match"`, `distance_km: null`). 404 if unknown or under 2 stations.

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

## Flood (GISTDA satellite)

GISTDA's flooded H3 cells (res 9, ~0.1 km² each) from the latest **3-day**, **7-day** and **30-day** satellite layers,
stored by `ingest/gistda_flood.py` (every 3 h when `GISTDA_API_KEY` is set). Credit GISTDA (สทอภ.) in the UI. Every
number is a count or sum of GISTDA's cells: `area_km2` is GISTDA's flooded area in the cell (the whole cell when it
gives none), `area_rai` = km² × 625, rounded. `period` is `3days` (default), `7days` or `30days`; anything else is 400.

Every answer carries the layer: `{ period, fetched_at, observed_from, observed_to }`. `fetched_at` is when we
downloaded it; `observed_*` is the span of GISTDA's satellite scenes in it (the 30-day layer: about a month). All three
are **null before the first download**: show "no data yet", never "no flooding". A downloaded layer with no cells is a
real "nothing detected".

**How lately water was seen.** GISTDA sends no date per cell, only the scenes of the whole layer. But the layers nest,
so a cell of the 30-day layer that is also in the 7-day one had water this week, and one also in the 3-day layer had
it in the last 3 days. `seen` (on `/cells`) and `last_seen` (on `/tambon`) are that: the shortest layer holding it.
A cell missing from the shorter layers may also simply have had no scene over it lately, so say "water last seen
…", never "dried up".

### `GET /api/flood/areas?period=3days&level=province&province=&amphoe=&limit=100` → `FloodAreas`
Flooded area per area, largest first, and the total over the same filter.
- `level`: `province | amphoe | tambon`. `province` / `amphoe` filter by name (no จ./อ. prefix) or GISTDA code.
- `limit` 1–1000. `lat`/`lon` is the area-weighted centre of the area's cells (to fly the map there).
- `buildings` / `population`: sums of GISTDA's counts in the area's cells; `null` when GISTDA sent none (unknown, not 0).
- An area GISTDA names only by code has `null` names and its `code` (`pv_idn` / `ap_idn` / `tb_idn`).

```jsonc
{ "period": "3days", "fetched_at": "…", "observed_from": "…", "observed_to": "…", "level": "amphoe",
  "total": { "cells": 1180, "area_km2": 121.4, "area_rai": 75875 },
  "areas": [ { "province": "พระนครศรีอยุธยา", "amphoe": "บางบาล", "tambon": null, "code": "1405",
               "cells": 412, "area_km2": 44.1, "area_rai": 27563, "lat": 14.38, "lon": 100.47,
               "buildings": 1120, "population": 3000 } ] }
```

### `GET /api/flood/cells?bbox=minLon,minLat,maxLon,maxLat&period=3days&res=9&limit=5000` → `FloodCells`
The flooded cells in the map's view, largest first. The web draws them with h3-js (`cellToBoundary`).
- `res` 9 = GISTDA's own cells, with `province`, `amphoe`, `tambon`, `observed_at` and GISTDA's counts in the cell:
  `buildings`, `population`, `schools`, `hospitals` (each `null` when not sent). `res` 5–8 sums them into parent
  cells for a zoomed-out map (`cells` = how many res-9 cells). `share` = flooded part of the cell, 0–1.
- `limit` 1–20000; `truncated: true` when the view holds more. `bbox` is required; a bad box is 400.
- `seen`: `3days | 7days | 30days`, the shortest layer that also holds the cell (for a parent, the freshest of its
  cells); never longer than `period`. `windows`: the layer fields of `period` and every shorter layer, shortest
  first, to label the map's legend with real dates.

```jsonc
{ "period": "30days", "fetched_at": "…", "observed_from": "…", "observed_to": "…", "res": 7, "truncated": false,
  "cells": [ { "h3": "8764a4770ffffff", "cells": 41, "area_km2": 4.9, "share": 0.93, "seen": "7days" } ],
  "windows": [ { "period": "3days", "fetched_at": "…", "observed_from": "…", "observed_to": "…" }, … ] }
```

### `GET /api/flood/near?lat=&lon=&period=3days` → `FloodNear`
Flooding around one point (the flood tab's saved-place banner); the same lookup the chat uses. `at_point`: the point's
own cell is flooded. `nearest_km`: the nearest flooded cell, `null` when none within `radius_km`. `nearby`: the flooded
cells within `radius_km`. Before the first download the layer fields are null and `nearby` is zero.

```jsonc
{ "period": "3days", "fetched_at": "…", "observed_from": "…", "observed_to": "…",
  "at_point": false, "nearest_km": 0.8, "radius_km": 5.0, "nearby": { "cells": 96, "area_km2": 7.9, "area_rai": 4938 } }
```

### `GET /api/flood/history?level=province|tambon&province=` → `FloodHistory`

Our copy of GISTDA's flood history (`ingest/gistda_history.py`, one row per sub-district), for the flood map's
history layer. `level=province` sums sub-districts per province; `level=tambon` needs `province` (pv code) and
lists its sub-districts. Codes are GISTDA's `pv_idn` / `tb_idn`, the same as the map outlines' `code`.

```
{ level, province, years: number[],          // every year the copy covers, e.g. 2011…2024
  provinces: string[],                       // pv codes copied so far
  areas: [{ code, province, amphoe, tambon,  // amphoe/tambon null at province level
            years: number[],                 // years with any flooding
            rai_by_year: { "2011": 3824.8 } }],  // GISTDA's own shape areas, summed; only flooded years
  source: "GISTDA" }
```
Sorted by the worst year's rai. An area missing from `areas` in a copied province never flooded in these years;
in a province not in `provinces` it is not known yet.

### `GET /api/flood/tambon?code=` → `FloodTambon`
One sub-district's flood story for the area panel: this season, our own record, and 2011–. `code` is GISTDA's 6-digit
`tb_idn` (the map outlines' `code`); not 6 digits is 400, a code no source knows is 404.
- `windows`: one per layer, shortest first, with the layer fields and that layer's cells / area / `buildings` /
  `population` in the sub-district (counts `null` = not sent). `last_seen`: the shortest layer with any cells there,
  `null` when none of the stored layers has any.
- `record`: our daily copy of the layers there (`flood_cells_history`), one row per day and period that had cells,
  oldest first. It starts on the day we started keeping it, so it is short at first: never extrapolate it.
- `history`: as `/api/flood/history` for this one sub-district; `years: []` = its province is copied and it never
  flooded 2011–; `history: null` = its province is not copied yet.

```jsonc
{ "code": "140504", "province": "พระนครศรีอยุธยา", "amphoe": "บางบาล", "tambon": "บางหลวงโดด",
  "windows": [ { "period": "3days", "fetched_at": "…", "observed_from": "…", "observed_to": "…",
                 "cells": 0, "area_km2": 0, "area_rai": 0, "buildings": null, "population": null }, … ],
  "last_seen": "7days",
  "record": [ { "day": "2026-10-20", "period": "30days", "cells": 412, "area_km2": 31.2, "area_rai": 19500 } ],
  "history": { "years_covered": [2011, …, 2024], "years": [2011, 2017], "rai_by_year": { "2011": 812.5 } },
  "source": "GISTDA" }
```

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
  "emergency": false      // true → UI shows the DDPM 1784 card above the reply
}
```

- **Station sources carry the reading** the web StationCard draws, so the card shows exactly the numbers the reply used:
  `{ type: "station", station_id, code, name_th, distance_km, observed_at, river, agency, amphoe, province,
  level_msl, diff_wl_bank, bank_msl, trend_cm_per_hr, hours_to_bank, risk_tier, upstream_over: string[], model }`.
  `diff_wl_bank` is signed (+ over bank); `bank_msl` = `level_msl − diff_wl_bank`; `trend_cm_per_hr` is the 3 h trend;
  `upstream_over` lists upstream station codes already over bank. Any of the reading fields can be null.
  `model` is the LightGBM forecast, `{ "<hours>": { level, diff_wl_bank, mae_cm } }`, only at the modelled corridor
  stations (null elsewhere). The reply text already says it; the LINE card shows it as a row.
- **GISTDA sources** (`{ type: "gistda", kind, summary, observed_at }`) repeat the reply's satellite lines:
  - `flood_extent`, from the stored 3-day layer (7-day when the 3-day one is empty): at a GPS point, flooded or not and
    the nearest flooded cell; around a place found by name (a district centre), the flooded area within 5 km. With
    nothing within 5 km now but water there in the 30-day layer, it says so ("น้ำอาจลดลงแล้ว").
    `observed_at` is the layer's newest GISTDA date, else when it was downloaded.
  - `flood_recurrence`: the years the subdistrict flooded, 2011–2024, from our copy of GISTDA's flood history
    (`ingest/gistda_history.py`); at a GPS point also whether the point itself flooded. `observed_at` null.
    Only for provinces already copied, and only when a historically flooded cell lies within 2 km.
  - `flood_extent` does not appear before the first layer is downloaded or without `GISTDA_API_KEY`.
- **Quick replies mirror LINE quick-reply actions** (`message` / `postback` / `location`). The same agent handler can serve the LINE webhook and the web.
- **Postback data the web sends today** (anything else is 400):
  - `consent:sensitive_memory:yes|no` → write `consents`
  - `memory:delete_all`
- **Required behaviour** (see the mock's `chatScript.ts` and its test for the full demo flow):
  - Danger messages return `emergency: true`, with 1784 first (on the web, the EmergencyCard above the reply carries it).
    NamMa files **no rescue request** on anyone's behalf: the reply says so, lists what to tell 1784 (the place, who
    needs help, how many people, from the message and memory) and what to do while waiting. No quick replies.
  - Web replies are plain text without emoji and without the hotline lines the UI already draws: no rescue 1784/1669 line when `emergency` is true, no DDPM footer when `sources` has a station (HotlineNotice). LINE replies keep both.
  - Ask for consent before saving vulnerable or health facts.
  - Say when a place is outside the demo basin instead of guessing.
  - Shelter questions ("ศูนย์พักพิงแถวบางบาล", "บ้านแม่ควรอพยพไปที่ไหน") list up to 3 shelters near the named place,
    else the saved place, with DDPM open/bedridden status, as `shelter` sources. No place known → ask for one.

## Profile

What NamMa remembers about the person chatting, for the web ProfilePanel (CLAUDE.md §7: view and delete).
Who: the signed-in account (LINE or Google), else the anonymous chat from the **`X-Chat-Session: <session_id>`** header (the same
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
  "line_linked": false                          // the account has a LINE id (gets LINE alerts)
}
```
- `approx`: the place was geocoded from its name, not a GPS share. `nearest` is null when no fresh station is within 60 km.
- `facts[].kind` is one of `household`, `needs` (sensitive), `house`, `assets`, `history`.

### `DELETE /api/profile/places/{id}` → `Profile`
404 if the place isn't this person's.

### `DELETE /api/profile/facts/{kind}` → `Profile`
`kind`: a fact kind, or `all` (every fact and place, and any fact waiting for consent).

## Auth

One account per person. It signs in with **LINE**, with **Google**, or with both linked, and keeps its saved places,
memory, consents and history across devices. LINE OA chatters already have an account (keyed by their LINE userId):
signing in on the web with LINE opens that same account.

`SessionUser = { display_name, picture_url, identities: { line: boolean, google: string | null } }`
(`google` is the linked Google account's email, shown on the account page only, never used to match accounts).

### `POST /api/auth/line` → `{ session_token, user: SessionUser, merged_chat: boolean }`
### `POST /api/auth/google` → same
Body: `{ id_token, chat_session? }`.
- **LINE:** a LIFF ID token, verified with `POST https://api.line.me/oauth2/v2.1/verify` (`client_id` = `LINE_LOGIN_CHANNEL_ID`). `sub` is the LINE userId.
- **Google:** a Google Sign-In ID token, verified with `https://oauth2.googleapis.com/tokeninfo`; `aud` must be `GOOGLE_CLIENT_ID` and the email verified. `sub` is the Google id.
- The first sign-in creates the account. A Google-created account has no LINE id until LINE is linked, so it gets no LINE pushes (alerts wait in its messages).
- **`chat_session`** is the anonymous web chat's `session_id`. Its places, memory, consents, pending facts and messages join the account and the anonymous chat is deleted (`merged_chat: true`). A place label both have keeps the anonymous chat's (newer) coordinates.
- **Demo (`DEMO_MODE=1`):** `demo:user`, `demo:user:<name>` (LINE, one account per name) and `demo:google:<name>` (Google). `<name>` is `[a-z0-9_-]{1,40}`.
- There are no staff accounts or roles (the responder role went with rescue tickets, 2026-10-19).

### `POST /api/auth/link/{line|google}` → `SessionUser` (signed in)
Body: `{ id_token }`. Adds a second way to sign in. If that LINE or Google account already has its own NamMa account (a LINE OA chatter, say), it is the same person: that account's places, memory and history join this one and it is deleted. **409 `conflict`** if this account already has a different one of that provider, or merging would give it two. 404 for another provider.

### `DELETE /api/auth/link/{line|google}` → `SessionUser`
Removes one way to sign in. **409** if it is the only one (delete the account instead).

### `POST /api/auth/logout` → `{ signed_out: true }`
Ends this session token.

### `GET /api/me` → `SessionUser`
Returns 401 without a session.

### `DELETE /api/me` → `{ deleted: true }`
PDPA erasure: the account, its sign-ins, sessions, places, memory, consents and messages. Signing in again starts an empty account.

## Demo controls (`DEMO_MODE=1` only)

`POST /api/demo/simulate { station_id, tier }` forces a tier and runs the alert job; `POST /api/demo/refresh` recomputes
real risk. Both need a signed-in session (demo login is enough) and answer 403 when `DEMO_MODE` is off.

## Known gaps

- **`station_edges`** has only RID stations, and `approx_travel_hours` is NULL until estimated.
- **GISTDA property names** are not documented. `ingest/gistda_flood.py` looks for each field under the names other
  projects report and keeps every page of each stored layer (`var/raw/gistda/flood`); check the parser against one.
