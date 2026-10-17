"""GISTDA flood extent + recurrence at a point. Needs GISTDA_API_KEY.

Response shapes are not inspected yet (CLAUDE.md §14), so every raw response is archived under
var/raw/gistda and the parser only reports what it can see for sure. Returns None without a key
or on any error: GISTDA is extra context, never a blocker for an answer.
"""
import json
import os
import time
from datetime import datetime

import httpx

from common.config import RAW_DIR

BASE = "https://api-gateway.gistda.or.th/api/2.0/resources/gi-service/v1.0/disasters"
CACHE_S = 3600
_cache: dict[tuple, tuple[float, dict | None]] = {}


def _get(kind: str, lat: float, lon: float) -> dict | None:
    key = os.environ.get("GISTDA_API_KEY")
    if not key:
        return None
    ck = (kind, round(lat, 3), round(lon, 3))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    try:
        r = httpx.get(f"{BASE}/{kind}", params={"lat": lat, "lon": lon, "api_key": key}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        data = None
    if data is not None:
        path = RAW_DIR / "gistda" / f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _cache[ck] = (time.time(), data)
    return data


def _features(data) -> list | None:
    if isinstance(data, dict):
        for k in ("features", "data", "result"):
            if isinstance(data.get(k), list):
                return data[k]
    return data if isinstance(data, list) else None


def check_flood_extent(lat: float, lon: float) -> dict | None:
    """{'flooded_last_1day': bool} when the response is a feature list; else None. TODO: parse real shape."""
    feats = _features(_get("flood-extent-1day", lat, lon))
    return None if feats is None else {"flooded_last_1day": len(feats) > 0, "source": "GISTDA"}


def get_flood_recurrence(lat: float, lon: float) -> dict | None:
    """{'records': n} of 2011–2023 flood records at the point. TODO: parse real shape (years/frequency)."""
    feats = _features(_get("flood-recurrence", lat, lon))
    return None if feats is None else {"records": len(feats), "source": "GISTDA (2011–2023)"}
