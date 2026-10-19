"""Agent tools (CLAUDE.md §6). Plain functions: typed in, plain dicts out, every number from the DB.

Each takes an open psycopg connection first so the caller controls transactions.
"""
import json
import time
from datetime import date, datetime, timedelta

from psycopg.rows import dict_row

from agent import gistda, risk
from agent.geo import haversine_km
from agent.risk import FRESH_HOURS
from ingest import store, thaiwater

MAX_STATION_KM = 60      # beyond this a river station says little about a house
FILL_TTL_S = 1800        # on-demand history fetch at most once per station per 30 min
_filled: dict[int, float] = {}


def _rows(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _fill_history(conn, station_ids: list[int]) -> bool:
    """Only the corridor is backfilled; any other station has just our live readings. Fetch its last
    ~2 days once so the trend is real. One attempt, no retry: this runs inside a chat reply."""
    got = False
    today = date.today()
    for sid in station_ids:
        if time.time() - _filled.get(sid, 0) < FILL_TTL_S:
            continue
        _filled[sid] = time.time()
        try:
            payload = thaiwater.fetch_waterlevel_graph(sid, today - timedelta(days=1), today, attempts=1)
            store.insert_history(conn, thaiwater.parse_waterlevel_graph(sid, payload))
            got = True
        except Exception as e:
            print(f"history fill failed for {sid}: {e!r}", flush=True)
    return got


def get_nearest_stations(conn, lat: float, lon: float, k: int = 3, fill: bool = True) -> list[dict]:
    """Nearest stations with a fresh reading, with level, bank gap, trend, tier and upstream status.
    fill=False skips the on-demand ThaiWater history fetch (for lists that must answer fast)."""
    out = _nearest(conn, lat, lon, k)
    if fill and _fill_history(conn, [s["station_id"] for s in out if s["trend_3h_cm_per_hr"] is None]):
        risk.compute_all(conn)
        out = _nearest(conn, lat, lon, k)
    return out


def _nearest(conn, lat: float, lon: float, k: int) -> list[dict]:
    cutoff = datetime.now().astimezone() - timedelta(hours=FRESH_HOURS)
    cands = _rows(conn, """
        SELECT s.id, s.oldcode, s.name_th, s.river, s.province, s.amphoe, s.agency, s.lat, s.lon,
               r.computed_at, r.level, r.trend_cm_per_hr, r.hours_to_bank, r.risk_tier, r.forecast_json
        FROM stations s JOIN station_risk r ON r.station_id = s.id
        WHERE s.lat IS NOT NULL AND (r.forecast_json->>'reading_ts')::timestamptz >= %s""", (cutoff,))
    for c in cands:
        c["distance_km"] = round(haversine_km(lat, lon, c["lat"], c["lon"]), 1)
    near = sorted((c for c in cands if c["distance_km"] <= MAX_STATION_KM), key=lambda c: c["distance_km"])[:k]
    out = []
    for c in near:
        f = c["forecast_json"] or {}
        up = _rows(conn, """
            SELECT s.id, s.oldcode, s.name_th, r.risk_tier, e.approx_travel_hours,
                   (r.forecast_json->>'diff_wl_bank')::float AS diff_wl_bank
            FROM station_edges e JOIN stations s ON s.id = e.upstream_id
            LEFT JOIN station_risk r ON r.station_id = s.id WHERE e.downstream_id = %s""", (c["id"],))
        out.append({
            "station_id": c["id"], "code": c["oldcode"], "name_th": c["name_th"], "river": c["river"],
            "province": c["province"], "amphoe": c["amphoe"], "agency": c["agency"],
            "distance_km": c["distance_km"], "lat": c["lat"], "lon": c["lon"],
            "reading_ts": f.get("reading_ts"), "level_msl": c["level"],
            "diff_wl_bank": f.get("diff_wl_bank"), "situation_level": f.get("situation_level"),
            "trend_3h_cm_per_hr": c["trend_cm_per_hr"], "trend_12h_cm_per_hr": f.get("trend_12h_cm_per_hr"),
            "hours_to_bank": c["hours_to_bank"], "risk_tier": c["risk_tier"], "upstream": up,
            "model": (f.get("model") or {}).get("horizons"),
        })
    return out


def get_forecast(conn, station_id: int) -> dict | None:
    rows = _rows(conn, "SELECT * FROM station_risk WHERE station_id = %s", (station_id,))
    return rows[0] if rows else None


def check_flood_extent(conn, lat: float, lon: float) -> dict | None:
    return gistda.check_flood_extent(conn, lat, lon)


def get_flood_recurrence(conn, lat: float, lon: float) -> dict | None:
    return gistda.get_flood_recurrence(conn, lat, lon)


SHELTER_BOX_DEG = 0.6            # ~65 km: look here first; the whole table only if nothing is that close
CLOSED_PENALTY_KM = 10.0         # rank an open shelter ahead of a closed one up to 10 km nearer
UNKNOWN_PATIENTS_PENALTY_KM = 10.0
NO_PATIENTS_PENALTY_KM = 30.0


def _patients_ok(r: dict) -> bool | None:
    """True / False / None (DDPM gave no answer). Manual rows only have accepts_patients."""
    if r.get("accepts_bedridden") is not None:
        return r["accepts_bedridden"]
    return True if r.get("accepts_patients") else None


def find_shelters(conn, lat: float, lon: float, needs: dict | None = None, k: int = 2) -> list[dict]:
    """Best shelters nearby, best first, each with distance_km and patients_ok.

    needs = {'patients': bool}. Ranking is distance plus penalties, never a hard filter, so a household always
    gets a shelter: closed now (+10 km), patients needed and unknown (+10 km) or refused (+30 km).
    'pets' is ignored: no source says which shelters take pets.
    """
    needs = needs or {}
    sql = "SELECT * FROM shelters WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s"
    d = SHELTER_BOX_DEG
    rows = _rows(conn, sql, (lat - d, lat + d, lon - d, lon + d)) or _rows(conn, "SELECT * FROM shelters")

    def score(r: dict) -> float:
        s = r["distance_km"] + (CLOSED_PENALTY_KM if r.get("is_open") is False else 0)
        if needs.get("patients"):
            ok = r["patients_ok"]
            s += 0 if ok else NO_PATIENTS_PENALTY_KM if ok is False else UNKNOWN_PATIENTS_PENALTY_KM
        return s

    for r in rows:
        r["distance_km"] = round(haversine_km(lat, lon, r["lat"], r["lon"]), 1)
        r["patients_ok"] = _patients_ok(r)
    return sorted(rows, key=lambda r: (score(r), r["distance_km"], r["id"]))[:k]


# --- users & memory ------------------------------------------------------------

def get_or_create_user(conn, external_id: str, display_name: str | None = None) -> int:
    """external_id is the LINE userId, or 'web:<uuid>' for anonymous web chat."""
    row = conn.execute(
        "INSERT INTO users (line_user_id, display_name) VALUES (%s, %s) ON CONFLICT (line_user_id) "
        "DO UPDATE SET display_name = COALESCE(EXCLUDED.display_name, users.display_name) RETURNING id",
        (external_id, display_name)).fetchone()
    conn.execute("INSERT INTO user_memory (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (row[0],))
    return row[0]


MEMORY_FIELDS = ("household", "house", "assets", "needs", "history", "preferences")


def merge_users(conn, from_id: int, into_id: int) -> bool:
    """Fold one user into another, then delete the first: an anonymous web chat ('web:<session>') joining the
    account that just signed in. Moves places (a label both have keeps the newer, anonymous coordinates),
    memory (field by field, the anonymous chat's facts win), consents (history kept, so the latest decision
    still wins), pending facts, messages and alerts. Returns False when there is nothing to do."""
    if from_id == into_id:
        return False
    # Works with any row factory (the API uses dict rows, the agent tuple rows): reads go through _rows.
    for loc in _rows(conn, "SELECT id, label, lat, lon, notes FROM user_locations WHERE user_id = %s", (from_id,)):
        same = _rows(conn, "SELECT id FROM user_locations WHERE user_id = %s AND label = %s", (into_id, loc["label"]))
        if same:
            conn.execute("UPDATE user_locations SET lat = %s, lon = %s, notes = %s WHERE id = %s",
                         (loc["lat"], loc["lon"], loc["notes"], same[0]["id"]))
            conn.execute("DELETE FROM user_locations WHERE id = %s", (loc["id"],))
        else:
            conn.execute("UPDATE user_locations SET user_id = %s WHERE id = %s", (into_id, loc["id"]))
    conn.execute("INSERT INTO user_memory (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (into_id,))
    sets = ", ".join(f"{f} = m.{f} || a.{f}" for f in MEMORY_FIELDS)
    conn.execute(f"UPDATE user_memory m SET {sets}, updated_at = now() FROM user_memory a "
                 "WHERE m.user_id = %s AND a.user_id = %s", (into_id, from_id))
    conn.execute("INSERT INTO pending_facts (user_id, facts) SELECT %s, facts FROM pending_facts WHERE user_id = %s "
                 "ON CONFLICT (user_id) DO UPDATE SET facts = pending_facts.facts || EXCLUDED.facts, ts = now()",
                 (into_id, from_id))
    for table in ("consents", "messages", "alerts_sent"):
        conn.execute(f"UPDATE {table} SET user_id = %s WHERE user_id = %s", (into_id, from_id))
    # Sign-in identities follow the person, unless the account already has one of that provider.
    conn.execute("UPDATE user_identities SET user_id = %s WHERE user_id = %s AND provider NOT IN "
                 "(SELECT provider FROM user_identities WHERE user_id = %s)", (into_id, from_id, into_id))
    line_from = _rows(conn, "SELECT line_user_id FROM users WHERE id = %s", (from_id,))[0]["line_user_id"]
    conn.execute("DELETE FROM users WHERE id = %s", (from_id,))   # cascades what is left (memory row, summary)
    if is_line_id(line_from):
        conn.execute("UPDATE users SET line_user_id = %s WHERE id = %s AND (line_user_id IS NULL "
                     "OR line_user_id LIKE 'web:%%')", (line_from, into_id))
    return True


def is_line_id(external_id: str | None) -> bool:
    """A real LINE userId (or a demo stand-in), not an anonymous web chat key."""
    return bool(external_id) and not external_id.startswith("web:")


def delete_user(conn, user_id: int) -> None:
    """PDPA erasure: the account and everything kept about the person (the rest cascades)."""
    conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


def has_consent(conn, user_id: int, kind: str = "sensitive_memory") -> bool:
    row = conn.execute("SELECT granted FROM consents WHERE user_id = %s AND kind = %s ORDER BY ts DESC LIMIT 1",
                       (user_id, kind)).fetchone()
    return bool(row and row[0])


def record_consent(conn, user_id: int, granted: bool, kind: str = "sensitive_memory") -> None:
    conn.execute("INSERT INTO consents (user_id, kind, granted) VALUES (%s, %s, %s)", (user_id, kind, granted))


def get_user_profile(conn, user_id: int) -> dict:
    mem = _rows(conn, "SELECT household, house, assets, needs, history, preferences FROM user_memory WHERE user_id = %s",
                (user_id,))
    locs = _rows(conn, "SELECT id, label, lat, lon, notes FROM user_locations WHERE user_id = %s ORDER BY id", (user_id,))
    pending = _rows(conn, "SELECT facts FROM pending_facts WHERE user_id = %s", (user_id,))
    return {"memory": mem[0] if mem else {}, "locations": locs,
            "pending": pending[0]["facts"] if pending else {}, "consent": has_consent(conn, user_id)}


def merge_memory(conn, user_id: int, facts: dict) -> None:
    """facts: {field: {key: value}} for the user_memory JSONB columns. Shallow merge per field."""
    for field, vals in facts.items():
        if field not in MEMORY_FIELDS or not vals:
            continue
        conn.execute(f"UPDATE user_memory SET {field} = {field} || %s::jsonb, updated_at = now() WHERE user_id = %s",
                     (json.dumps(vals, ensure_ascii=False), user_id))


def set_pending(conn, user_id: int, facts: dict) -> None:
    conn.execute("INSERT INTO pending_facts (user_id, facts) VALUES (%s, %s) ON CONFLICT (user_id) "
                 "DO UPDATE SET facts = pending_facts.facts || EXCLUDED.facts, ts = now()",
                 (user_id, json.dumps(facts, ensure_ascii=False)))


def pop_pending(conn, user_id: int) -> dict:
    row = conn.execute("DELETE FROM pending_facts WHERE user_id = %s RETURNING facts", (user_id,)).fetchone()
    return row[0] if row else {}


def forget(conn, user_id: int, what: str) -> None:
    """what: 'all', a memory field name, or 'location:<id>'."""
    if what == "all":
        conn.execute("UPDATE user_memory SET household='{}', house='{}', assets='{}', needs='{}', history='{}', "
                     "updated_at=now() WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM user_locations WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM pending_facts WHERE user_id = %s", (user_id,))
    elif what.startswith("location:"):
        conn.execute("DELETE FROM user_locations WHERE user_id = %s AND id = %s", (user_id, int(what.split(":")[1])))
    elif what in ("household", "house", "assets", "needs", "history"):
        conn.execute(f"UPDATE user_memory SET {what} = '{{}}', updated_at = now() WHERE user_id = %s", (user_id,))


def save_location(conn, user_id: int, label: str, lat: float, lon: float, notes: str | None = None) -> int:
    """Upsert by label so "บ้านแม่" said twice stays one place."""
    row = conn.execute("SELECT id FROM user_locations WHERE user_id = %s AND label = %s", (user_id, label)).fetchone()
    if row:
        conn.execute("UPDATE user_locations SET lat = %s, lon = %s, notes = COALESCE(%s, notes) WHERE id = %s",
                     (lat, lon, notes, row[0]))
        return row[0]
    return conn.execute("INSERT INTO user_locations (user_id, label, lat, lon, notes) VALUES (%s, %s, %s, %s, %s) "
                        "RETURNING id", (user_id, label, lat, lon, notes)).fetchone()[0]


def log_message(conn, user_id: int, channel: str, role: str, content: str) -> int:
    return conn.execute("INSERT INTO messages (user_id, channel, role, content) VALUES (%s, %s, %s, %s) RETURNING id",
                        (user_id, channel, role, content)).fetchone()[0]


def recent_messages(conn, user_id: int, n: int = 10) -> list[dict]:
    rows = _rows(conn, "SELECT role, content FROM messages WHERE user_id = %s AND role IN ('user', 'assistant') "
                       "ORDER BY ts DESC LIMIT %s", (user_id, n))
    return rows[::-1]
