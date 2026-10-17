"""Rule-based station risk (CLAUDE.md §8). The ML forecast is an add-on, not a dependency.

    python -m agent.risk        # recompute station_risk for every station with a recent reading

Tiers: normal → watch (rising and < 0.5 m to bank) → warning (< 6 h to bank, or upstream over bank)
→ danger (over bank, or situation_level 5).
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.db import connect

TIERS = ["normal", "watch", "warning", "danger"]
TIER_TH = {"normal": "ปกติ", "watch": "เฝ้าระวัง", "warning": "เตือนภัย", "danger": "อันตราย"}
WATCH_GAP_M = 0.5
WARNING_HOURS = 6.0
RISING_CM_PER_HR = 0.5        # below this we call it steady
FRESH_HOURS = 6               # ignore stations whose last reading is older than this


def rank(tier: str | None) -> int:
    return TIERS.index(tier) if tier in TIERS else -1


@dataclass
class StationRisk:
    station_id: int
    computed_at: datetime
    reading_ts: datetime
    level: float
    diff_wl_bank: float | None     # signed, + above bank
    situation_level: int | None
    trend_cm_per_hr: float | None  # last 3 h
    trend_12h_cm_per_hr: float | None
    hours_to_bank: float | None
    upstream_over_bank: list[int]
    risk_tier: str

    def forecast_json(self) -> dict:
        # Linear extrapolation of the 3 h trend, labelled as such. Replaced by the model if trained.
        out = {"method": "linear_trend_3h", "reading_ts": self.reading_ts.isoformat(),
               "diff_wl_bank": self.diff_wl_bank, "situation_level": self.situation_level,
               "trend_12h_cm_per_hr": self.trend_12h_cm_per_hr, "upstream_over_bank": self.upstream_over_bank}
        if self.trend_cm_per_hr is not None:
            for h in (6, 12):
                out[f"level_plus_{h}h"] = round(self.level + self.trend_cm_per_hr * h / 100, 2)
        return out


def trend(readings: list[tuple[datetime, float]], hours: float) -> float | None:
    """cm/hr over the last `hours`, least squares. None if the window is too thin to say."""
    if not readings:
        return None
    end = readings[-1][0]
    pts = [(t, v) for t, v in readings if t >= end - timedelta(hours=hours)]
    if len(pts) < 2 or (pts[-1][0] - pts[0][0]) < timedelta(hours=hours / 3):
        return None
    xs = [(t - pts[0][0]).total_seconds() / 3600 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(100 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 2)


def classify(diff_wl_bank: float | None, situation_level: int | None, trend_cm: float | None,
             upstream_over_bank: bool) -> tuple[str, float | None]:
    """Returns (tier, hours_to_bank)."""
    hours_to_bank = None
    if diff_wl_bank is not None and diff_wl_bank < 0 and trend_cm is not None and trend_cm >= RISING_CM_PER_HR:
        hours_to_bank = round(-diff_wl_bank * 100 / trend_cm, 1)
    if (diff_wl_bank is not None and diff_wl_bank >= 0) or situation_level == 5:
        return "danger", hours_to_bank
    if (hours_to_bank is not None and hours_to_bank < WARNING_HOURS) or upstream_over_bank:
        return "warning", hours_to_bank
    rising = trend_cm is not None and trend_cm >= RISING_CM_PER_HR
    if rising and diff_wl_bank is not None and -diff_wl_bank < WATCH_GAP_M:
        return "watch", hours_to_bank
    return "normal", hours_to_bank


def assess(station_id: int, readings: list[tuple[datetime, float, float | None, int | None]],
           upstream_over_bank: list[int], now: datetime) -> StationRisk:
    """readings: (ts, level_msl, diff_wl_bank, situation_level), oldest first."""
    ts, level, diff, sit = readings[-1]
    series = [(t, lv) for t, lv, _, _ in readings]
    t3, t12 = trend(series, 3), trend(series, 12)
    tier, h = classify(diff, sit, t3, bool(upstream_over_bank))
    return StationRisk(station_id, now, ts, level, diff, sit, t3, t12, h, upstream_over_bank, tier)


# --- DB job ------------------------------------------------------------------

def _load(conn, now: datetime):
    rows = conn.execute(
        "SELECT station_id, ts, level_msl, diff_wl_bank, situation_level FROM waterlevel_readings "
        "WHERE ts >= %s AND level_msl IS NOT NULL ORDER BY station_id, ts", (now - timedelta(hours=13),)).fetchall()
    by_station: dict[int, list] = {}
    for sid, ts, lv, diff, sit in rows:
        by_station.setdefault(sid, []).append((ts, lv, diff, sit))
    return by_station


def compute_all(conn, now: datetime | None = None) -> list[StationRisk]:
    now = now or datetime.now().astimezone()
    by_station = {sid: r for sid, r in _load(conn, now).items() if r[-1][0] >= now - timedelta(hours=FRESH_HOURS)}
    edges = conn.execute("SELECT upstream_id, downstream_id FROM station_edges").fetchall()
    over = {sid for sid, r in by_station.items()
            if (r[-1][2] is not None and r[-1][2] >= 0) or r[-1][3] == 5}
    upstream = {}
    for up, down in edges:
        if up in over:
            upstream.setdefault(down, []).append(up)
    results = [assess(sid, r, upstream.get(sid, []), now) for sid, r in by_station.items()]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO station_risk (station_id, computed_at, level, trend_cm_per_hr, hours_to_bank, risk_tier, forecast_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (station_id) DO UPDATE SET computed_at = EXCLUDED.computed_at, "
            "level = EXCLUDED.level, trend_cm_per_hr = EXCLUDED.trend_cm_per_hr, hours_to_bank = EXCLUDED.hours_to_bank, "
            # Keep the last model forecast (written by model.forecast.score) across rule refreshes.
            "risk_tier = EXCLUDED.risk_tier, forecast_json = EXCLUDED.forecast_json || "
            "jsonb_strip_nulls(jsonb_build_object('model', station_risk.forecast_json->'model'))",
            [(r.station_id, r.computed_at, r.level, r.trend_cm_per_hr, r.hours_to_bank, r.risk_tier,
              json.dumps(r.forecast_json())) for r in results])
    return results


def main() -> None:
    with connect() as conn:
        results = compute_all(conn)
    counts = {t: sum(r.risk_tier == t for r in results) for t in TIERS}
    print(f"station_risk: {len(results)} stations {counts}")


if __name__ == "__main__":
    main()
