# `/api/rivers`: every river, upstream first (design draft)

Status: backend built 2026-10-20 (`ingest/rivers.py`, `api/rivers.py`, migration 009; contract in `docs/api.md` →
Rivers). Changes from the draft: clusters use 150 km, not 80 (the Ping has a 113 km gap across Bhumibol, the Nan 94 km
across Sirikit), and overrides give a `sort_bank` instead of an order number. Replaces the corridor-only `GET /api/stations` as the source for the
home page's river charts (staff gauges, river line, cross-sections). The map, chat, forecast and upstream links
are unchanged.

## Why

The home charts show at most 22 stations on 4 rivers, because `/api/stations` returns only
`data/corridor_stations.csv`. Meanwhile ingest already stores all ~800 ThaiWater stations with `river`,
`basin` and `min_bank`. The corridor CSV is hand-made and doesn't scale. It's also wrong in one place:
C.35 and CPY011 are 10 m apart but have different `stem_order` values (10 and 11).

## What the live data supports (ThaiWater `waterlevel_load`, 2026-10-19 23:08)

| | |
|---|---|
| Stations | 804 (HII 327, RID 314, FOP 91, EGAT 72) |
| With a `river_name` | 722; the 82 without are mostly FOP / HII / EGAT, only 1 RID |
| With `min_bank` | 804; 6 have `min_bank <= 0` (bad data) |
| River names | 327; 62 have ≥ 3 stations |
| Same-river pairs < 2 km apart | 86 (mostly an RID and an HII gauge at one place, e.g. C.35/CPY011, Kgt.6/PRC005, Y.15/YOM007) |

**Ordering by bank elevation works.** Rank correlation between "sorted by `min_bank`, highest first" and the
true downstream order:

| River | Stations | Spearman ρ | Checked against |
|---|---|---|---|
| Chao Phraya | 18 | 0.996 | corridor `stem_order` |
| Chi | 22 | 0.997 | longitude (flows east) |
| Nan | 25 | 0.997 | latitude (flows south) |
| Ping | 20 | 0.997 | latitude |
| Yom | 24 | 0.993 | latitude |
| Mun | 22 | 0.977 | longitude (flows east) |

The remaining swaps are mostly co-located pairs from different agencies whose bank surveys disagree by up to
1 m (C.35 4.35 m vs CPY011 5.06 m). Removing duplicates *before* sorting removes most of them.

Weak spot: the tidal reach near Bangkok, where the bottom four Chao Phraya banks are 2.62 → 2.16 m. The order
is still right, but a 10 cm survey error would flip it.

## Pipeline (in ingest, after each `waterlevel_load`)

1. **Group.** Key by `river` name, then split each name into clusters: stations stay together if each is
   within 150 km of another in the cluster. This separates generic names that are different streams
   (`คลองใหญ่` appears in 3 basins, `ห้วยยาง` in 2). It also keeps a real river together across a basin
   boundary (the Tha Chin and Pa Sak are each tagged with two basins). Stations with no `river` name stay
   off the river charts. They are still on the map and in nearest-station answers.
2. **Remove duplicates.** Within a cluster, stations < 3 km apart are one place. Prefer RID: its banks are
   the surveyed ones, and the risk rules were tuned on RID. If RID's latest reading is older than 3 h and
   another agency's is fresh, prefer the fresh one. Keep the others as `same_place_as` so the map can still
   show them. (On the Chao Phraya the closest *distinct* places are 8 km apart, so 3 km is safe there. Check
   it on 2–3 more rivers.)
3. **Order.** Sort by `min_bank` descending, after dropping `min_bank <= 0`.
4. **Override.** `data/river_overrides.csv` (`station_id, river_key, sort_bank, exclude`) beats steps 1–3. Use it
   for tidal reaches, branches (Noi, Bang Ban) and anything a person checked by hand. It starts empty: on the live
   data the automatic order already matches the corridor's hand-made order on the Chao Phraya.
5. **Optional, later:** where an override isn't enough, project stations onto the river's line from
   HydroRIVERS or OSM and sort by distance along it. Only do this if the bank sort keeps getting a river
   wrong.

Stored on `stations`: `river_key TEXT`, `river_order INT`, `same_place_as INT NULL`. Recomputed each ingest
(`agent.jobs`, in a savepoint) and by `common.seed`. On the 2026-10-19 data: 716 stations on rivers, 92 stand-ins,
93 rivers with ≥ 2 stations; `/api/rivers` answers in ~30 ms (31 KB).

## API

```
GET /api/rivers?lat=&lon=&limit=6
```

Returns rivers with ≥ 2 stations after removing duplicates. They are ordered as follows:

1. rivers with any station at `watch` or worse, worst tier first, then by how far over the bank;
2. if `lat`/`lon` are given, the river nearest that point, even if it is calm;
3. nothing else. The page offers search for the rest.

```jsonc
{
  "data": [
    {
      "key": "แม่น้ำเจ้าพระยา",              // river name; a second same-named river is "<name>@<lowest station id>"
      "name_th": "แม่น้ำเจ้าพระยา",
      "basins": ["ลุ่มน้ำเจ้าพระยา"],
      "worst_tier": "danger",
      "reason": "at_risk",                  // at_risk | near_you | largest | match
      "stations": [ /* StationSummary, upstream first, duplicates removed */ ]
    }
  ],
  "meta": { "ingested_at": "…" }
}

GET /api/rivers/{key}        // one river, all its stations
GET /api/rivers?q=ยม         // search by name, for the picker
```

`StationSummary` is the existing shape. `stem_order` and `role` stay as they are for the corridor; the frontend
stops reading them for ordering.

## Frontend (namma-web)

- `RiverProfile` takes `River[]` from `/api/rivers` directly. `riverGroups()` goes away; it currently removes
  duplicates on the client as a stopgap.
- The home page passes the saved place's `lat`/`lon` when it has one.
- Long rivers (Nan, 25 stations): the staff gauges already scroll sideways. Cross-sections and the river line
  should show the ~8 stations nearest the user, plus a "whole river" link.
- Mock: `export_web_fixtures` exports the `/api/rivers` response for 3–4 rivers, not just the corridor.

## Open questions

- Should we trust FOP / EGAT stations on the charts? EGAT ones are often dam tailwater stations, which react to
  releases, not rain.
- Branches: the bank sort orders stations *within* a river but can't say that the Noi leaves the Chao Phraya
  at C.13. That stays in `station_edges.csv` and the overrides.
- 3 km duplicate radius vs. a short side channel: check on the Tha Chin (T.1 / THA008 are 1.5 km apart).
