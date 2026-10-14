"""ThaiWater (HII) client. The only module that talks to api-v3.thaiwater.net.

The endpoints are public but undocumented; see CLAUDE.md §4 for what we verified.
Fetchers return raw JSON. parse_* functions turn it into plain dicts and are pure,
so they're tested against trimmed real responses in tests/fixtures.
"""
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

BASE = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public"
BKK = ZoneInfo("Asia/Bangkok")
MIN_INTERVAL_S = 1.0          # be polite: at most one request per second
MAX_HISTORY_DAYS = 366        # longer ranges are silently truncated by the API

_last_call = 0.0


def _get(path: str, params: dict | None = None) -> dict:
    global _last_call
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    r = httpx.get(f"{BASE}/{path}", params=params, timeout=60,
                  headers={"User-Agent": "NamMa flood assistant (hackathon)"})
    r.raise_for_status()
    return r.json()


def fetch_waterlevel_load() -> dict:
    return _get("waterlevel_load")


def fetch_rain_24h() -> dict:
    return _get("rain_24h")


def fetch_waterlevel_graph(station_id: int, start: date, end: date) -> dict:
    return _get("waterlevel_graph", {
        "station_type": "tele_waterlevel",
        "station_id": station_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    })


def fetch_waterlevel_history(station_id: int, start: date, end: date) -> list[dict]:
    """History for any range, fetched in chunks the API won't truncate."""
    readings = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=MAX_HISTORY_DAYS - 1), end)
        readings += parse_waterlevel_graph(station_id, fetch_waterlevel_graph(station_id, chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return readings


# --- parsing -----------------------------------------------------------------

def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(s: str) -> datetime:
    return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BKK)


def _th(d: dict | None, key: str, lang: str = "th") -> str | None:
    v = (d or {}).get(key) or {}
    return v.get(lang) or None


def signed_diff_wl_bank(value, text: str | None) -> float | None:
    """ThaiWater gives a magnitude plus Thai text; we store + above bank, - below."""
    v = _num(value)
    if v is None:
        return None
    text = text or ""
    if "ล้นตลิ่ง" in text:
        return abs(v)
    if "ต่ำกว่าตลิ่ง" in text:
        return -abs(v)
    return v


def parse_waterlevel_load(payload: dict) -> tuple[list[dict], list[dict]]:
    """Returns (stations, readings) from waterlevel_load."""
    stations, readings = [], []
    for r in payload["waterlevel_data"]["data"]:
        s = r.get("station") or {}
        if not s.get("id"):
            continue
        g = r.get("geocode") or {}
        stations.append({
            "id": s["id"],
            "oldcode": s.get("tele_station_oldcode"),
            "name_th": _th(s, "tele_station_name"),
            "name_en": _th(s, "tele_station_name", "en"),
            "lat": _num(s.get("tele_station_lat")),
            "lon": _num(s.get("tele_station_long")),
            "basin": _th(r.get("basin"), "basin_name"),
            "river": r.get("river_name"),
            "province": _th(g, "province_name"),
            "amphoe": _th(g, "amphoe_name"),
            "tambon": _th(g, "tumbon_name"),
            "left_bank": _num(s.get("left_bank")),
            "right_bank": _num(s.get("right_bank")),
            "min_bank": _num(s.get("min_bank")),
            "ground_level": _num(s.get("ground_level")),
            "critical_level_msl": _num(s.get("critical_level_msl")),
            "agency": _th(r.get("agency"), "agency_shortname", "en"),
            "is_key_station": bool(s.get("is_key_station")),
        })
        if r.get("waterlevel_datetime") and _num(r.get("waterlevel_msl")) is not None:
            readings.append({
                "station_id": s["id"],
                "ts": _ts(r["waterlevel_datetime"]),
                "level_msl": _num(r["waterlevel_msl"]),
                "diff_wl_bank": signed_diff_wl_bank(r.get("diff_wl_bank"), r.get("diff_wl_bank_text")),
                "situation_level": r.get("situation_level"),
                "discharge": _num(r.get("discharge")),
            })
    return stations, readings


def parse_waterlevel_graph(station_id: int, payload: dict) -> list[dict]:
    """History readings; nulls dropped. diff_wl_bank is derived from min_bank when known."""
    data = payload["data"]
    min_bank = _num(data.get("min_bank"))
    out = []
    for p in data["graph_data"]:
        level = _num(p.get("value"))
        if level is None:
            continue
        out.append({
            "station_id": station_id,
            "ts": _ts(p["datetime"]),
            "level_msl": level,
            "diff_wl_bank": round(level - min_bank, 3) if min_bank is not None else None,
            "situation_level": None,
            "discharge": _num(p.get("discharge")),
        })
    return out


def parse_rain_24h(payload: dict) -> tuple[list[dict], list[dict]]:
    """Returns (rain_stations, rain_readings) from rain_24h."""
    stations, readings = [], []
    for r in payload["data"]:
        s = r.get("station") or {}
        if not s.get("id"):
            continue
        stations.append({
            "id": s["id"],
            "oldcode": s.get("tele_station_oldcode"),
            "name_th": _th(s, "tele_station_name"),
            "lat": _num(s.get("tele_station_lat")),
            "lon": _num(s.get("tele_station_long")),
            "province": _th(r.get("geocode"), "province_name"),
            "basin": _th(r.get("basin"), "basin_name"),
        })
        if r.get("rainfall_datetime"):
            readings.append({
                "station_id": s["id"],
                "ts": _ts(r["rainfall_datetime"]),
                "rain_1h": _num(r.get("rain_1h")),
                "rain_24h": _num(r.get("rain_24h")),
            })
    return stations, readings
