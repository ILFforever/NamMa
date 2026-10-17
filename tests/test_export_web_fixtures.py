from datetime import datetime, timedelta

from ingest.export_web_fixtures import hourly, provisional_risk, trend_cm_per_hr
from ingest.thaiwater import BKK

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=BKK)


def test_provisional_risk_tiers():
    assert provisional_risk(None, 5.0, False) is None
    assert provisional_risk(0.15, -2.0, False) == ("danger", None)          # over bank, even if falling
    assert provisional_risk(-0.20, 5.0, False) == ("warning", 4.0)          # 20 cm at 5 cm/h → 4 h
    assert provisional_risk(-2.0, -1.0, True)[0] == "warning"               # upstream already over bank
    assert provisional_risk(-0.40, 1.0, False) == ("watch", 40.0)           # rising, < 0.5 m to bank
    assert provisional_risk(-0.40, 0.2, False)[0] == "normal"               # flat counts as not rising
    assert provisional_risk(-1.50, 3.0, False) == ("normal", 50.0)


def test_trend_cm_per_hr():
    rows = [{"ts": T0 + timedelta(hours=h), "level_msl": 4.0 + 0.03 * h} for h in range(5)]
    assert trend_cm_per_hr(rows) == 3.0
    assert trend_cm_per_hr(rows[:1]) is None
    # gap: nothing near 3 h before the latest reading
    assert trend_cm_per_hr([rows[0], {"ts": T0 + timedelta(hours=10), "level_msl": 5.0}]) is None


def test_hourly_keeps_first_reading_per_hour():
    rows = [{"ts": T0 + timedelta(minutes=m), "level_msl": m} for m in (0, 10, 20, 60, 70)]
    assert [r["level_msl"] for r in hourly(rows)] == [0, 60]
