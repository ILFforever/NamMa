from datetime import datetime, timedelta

from agent import risk
from ingest.thaiwater import BKK

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=BKK)


def series(levels, step_min=60):
    return [(T0 + timedelta(minutes=step_min * i), lv) for i, lv in enumerate(levels)]


def test_trend_cm_per_hr():
    assert risk.trend(series([4.00, 4.02, 4.04, 4.06]), 3) == 2.0
    assert risk.trend(series([4.0]), 3) is None                  # one point says nothing
    assert risk.trend(series([4.0, 4.1], step_min=10), 3) is None  # window too short


def test_classify_tiers():
    assert risk.classify(0.10, None, None, False)[0] == "danger"        # over bank
    assert risk.classify(-1.0, 5, None, False)[0] == "danger"           # situation level 5
    tier, h = risk.classify(-0.20, None, 5.0, False)                   # 20 cm at 5 cm/h = 4 h
    assert (tier, h) == ("warning", 4.0)
    assert risk.classify(-2.0, None, 0.0, True)[0] == "warning"         # upstream over bank
    assert risk.classify(-0.40, None, 1.0, False)[0] == "watch"         # rising, < 0.5 m, 40 h away
    assert risk.classify(-0.40, None, -1.0, False)[0] == "normal"       # falling
    assert risk.classify(-3.0, None, 2.0, False)[0] == "normal"


def test_assess_uses_latest_reading():
    rows = [(t, lv, lv - 5.0, None) for t, lv in series([4.70, 4.75, 4.80, 4.85])]
    r = risk.assess(1, rows, [], T0 + timedelta(hours=4))
    assert r.level == 4.85 and r.trend_cm_per_hr == 5.0
    assert r.hours_to_bank == 3.0 and r.risk_tier == "warning"
    assert r.forecast_json()["level_plus_6h"] == 5.15


def test_jump_then_falling_is_not_rising():
    # AIT001 (Khlong Saen Saep), 2026-10-20: slow fall, a 92 cm jump at 08:20, then falling for an hour.
    levels = [1.29, 1.28, 1.27, 1.26, 1.25, 1.25, 1.25, 2.17, 2.28, 2.20, 2.01, 1.92, 1.83, 1.78, 1.72]
    banks = 2.39
    rows = [(t, lv, round(lv - banks, 2), None) for t, lv in series(levels, step_min=10)]
    r = risk.assess(1, rows, [], rows[-1][0])
    assert r.trend_cm_per_hr < 0 and r.hours_to_bank is None
    assert r.risk_tier == "normal"
    # A steady rise keeps its 3 h trend.
    steady = [(t, 1.0 + 0.05 * i, -1.0 + 0.05 * i, None) for i, (t, _) in enumerate(series([0] * 15, step_min=10))]
    assert risk.assess(1, steady, [], steady[-1][0]).trend_cm_per_hr == 30.0
