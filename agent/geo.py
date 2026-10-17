"""Distance math and Thai place-name lookup (data/gazetteer.csv, built by ingest.gazetteer)."""
import csv
import math
from dataclasses import dataclass
from functools import lru_cache

from common.config import ROOT

GAZETTEER_CSV = ROOT / "data" / "gazetteer.csv"

# Short forms people actually type → official province names in ThaiWater geocode.
PROVINCE_ALIASES = {
    "อยุธยา": "พระนครศรีอยุธยา", "กรุงเทพ": "กรุงเทพมหานคร", "กทม": "กรุงเทพมหานคร",
    "โคราช": "นครราชสีมา", "สุพรรณ": "สุพรรณบุรี", "นครสวรรค์": "นครสวรรค์", "พิษณุโลก": "พิษณุโลก",
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Place:
    province: str
    amphoe: str
    tambon: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        parts = []
        if self.tambon:
            parts.append(f"ต.{self.tambon}")
        if self.amphoe:
            parts.append(f"อ.{self.amphoe}")
        parts.append(f"จ.{self.province}")
        return " ".join(parts)


@lru_cache(maxsize=1)
def _gazetteer() -> list[Place]:
    if not GAZETTEER_CSV.exists():
        return []
    with open(GAZETTEER_CSV, encoding="utf-8") as f:
        return [Place(r["province"], r["amphoe"], r["tambon"], float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)]


def find_place(text: str) -> Place | None:
    """Best (most specific, mutually consistent) place named in free Thai text, or None.

    Thai has no spaces, so we substring-match every known name. Names shorter than 3 chars are
    skipped to avoid false hits. A district only counts if its province is named or unambiguous.
    """
    text = text.replace(" ", "")
    provinces = {PROVINCE_ALIASES.get(a, a) for a in PROVINCE_ALIASES if a in text}
    places = _gazetteer()
    provinces |= {p.province for p in places if not p.amphoe and p.province in text}

    def hit(name: str) -> bool:
        return len(name) >= 3 and name in text

    best, best_score = None, 0
    for p in places:
        score = 0
        if p.tambon:
            # A bare tambon name is often shared across provinces; trust it only with context.
            if not hit(p.tambon) or not (hit(p.amphoe) or p.province in provinces):
                continue
            score = 3 + (1 if hit(p.amphoe) else 0)
        elif p.amphoe:
            if not hit(p.amphoe):
                continue
            score = 2
        else:
            score = 1 if p.province in provinces else 0
        if provinces:
            score = score + 2 if p.province in provinces else score - 2
        if score > best_score:
            best, best_score = p, score
    return best
