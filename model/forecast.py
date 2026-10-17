"""LightGBM level forecast (CLAUDE.md §9): level change at +6/+12/+24/+48 h for stations with upstream edges.

    python -m model.forecast train      # train on history before TEST_START, test on Sept–Oct 2026 held out
    python -m model.forecast score      # write predictions into station_risk.forecast_json['model']

Features at hour t: own bank gap and rises over 1–24 h; the same for upstream stations up to two hops
(via station_edges); dam outflow at C.13 when it is upstream. Target: level(t+h) − level(t).
A horizon is used only if it beat persistence on the held-out test, so a weak model never speaks.
"""
import json
import sys
from datetime import datetime, timedelta

import numpy as np

from common.config import ROOT
from common.db import connect

HORIZONS = (6, 12, 24, 48)
RISES = (1, 3, 6, 12, 24)
DAM = 2744                       # C.13 below Chao Phraya Dam: its discharge is the main control
TEST_START = datetime.fromisoformat("2026-09-01T00:00:00+07:00")
ART = ROOT / "model" / "artifacts"
PARAMS = {"objective": "l1", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 50,
          "feature_fraction": 0.9, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1}
ROUNDS = 300


def _edges(conn) -> dict[int, list[int]]:
    up: dict[int, list[int]] = {}
    for u, d in conn.execute("SELECT upstream_id, downstream_id FROM station_edges"):
        up.setdefault(d, []).append(u)
    return up


def ancestors(up: dict[int, list[int]], sid: int, hops: int = 2) -> list[int]:
    out, frontier = [], [sid]
    for _ in range(hops):
        frontier = [u for s in frontier for u in up.get(s, [])]
        out += frontier
    return out


def load_hourly(conn, sid: int, since: datetime | None = None) -> dict:
    """{hour: (level, diff_wl_bank, discharge)} using the last reading in each hour."""
    sql = "SELECT ts, level_msl, diff_wl_bank, discharge FROM waterlevel_readings WHERE station_id = %s"
    params: tuple = (sid,)
    if since:
        sql += " AND ts >= %s"
        params += (since,)
    out = {}
    for ts, lv, diff, q in conn.execute(sql + " ORDER BY ts", params):
        out[ts.replace(minute=0, second=0, microsecond=0)] = (lv, diff, q)
    return out


def _arrays(series: dict, hours: list[datetime]) -> np.ndarray:
    """(len(hours), 3) array of level, diff, discharge; NaN where missing."""
    return np.array([series.get(h, (None, None, None)) for h in hours], dtype=float)


def _rises(level: np.ndarray, i: np.ndarray, ks) -> list[np.ndarray]:
    return [level[i] - level[np.clip(i - k, 0, None)] for k in ks]


def features(data: dict[int, np.ndarray], sid: int, ups: list[int], i: np.ndarray) -> np.ndarray:
    own = data[sid]
    cols = [own[i, 1]] + _rises(own[:, 0], i, RISES)
    for u in ups:
        a = data[u]
        cols += [a[i, 1]] + _rises(a[:, 0], i, (3, 6, 12, 24))
    if DAM in ups:
        q = data[DAM][:, 2]
        cols += [q[i], q[i] - q[np.clip(i - 24, 0, None)]]
    return np.column_stack(cols)


def _dataset(conn, sid: int, up: dict) -> tuple[list[datetime], dict[int, np.ndarray], list[int]]:
    ups = ancestors(up, sid)
    raw = {s: load_hourly(conn, s) for s in [sid] + ups}
    start, end = min(raw[sid]), max(raw[sid])
    hours = [start + timedelta(hours=n) for n in range(int((end - start).total_seconds() // 3600) + 1)]
    return hours, {s: _arrays(r, hours) for s, r in raw.items()}, ups


def train() -> None:
    import lightgbm as lgb
    ART.mkdir(parents=True, exist_ok=True)
    report = {}
    with connect() as conn:
        up = _edges(conn)
        for sid in sorted(up):
            hours, data, ups = _dataset(conn, sid, up)
            level = data[sid][:, 0]
            idx = np.arange(len(hours))
            is_test = np.array([h >= TEST_START for h in hours])
            X = features(data, sid, ups, idx)
            report[sid] = {"upstream": ups}
            for h in HORIZONS:
                y = np.full(len(hours), np.nan)
                y[:-h] = level[h:] - level[:-h]
                ok = ~np.isnan(y) & ~np.isnan(level)
                tr, te = ok & ~is_test, ok & is_test
                if tr.sum() < 2000 or te.sum() < 100:
                    report[sid][h] = {"skipped": f"train={int(tr.sum())} test={int(te.sum())}"}
                    continue
                model = lgb.train(PARAMS, lgb.Dataset(X[tr], y[tr]), ROUNDS)
                pred = model.predict(X[te])
                mae = float(np.mean(np.abs(pred - y[te])) * 100)
                base = float(np.mean(np.abs(y[te])) * 100)          # persistence predicts zero change
                report[sid][h] = {"mae_cm": round(mae, 1), "persistence_mae_cm": round(base, 1),
                                  "n_train": int(tr.sum()), "n_test": int(te.sum()), "use": mae < base}
                model.save_model(str(ART / f"{sid}_{h}h.txt"))
                print(f"station {sid} +{h:>2}h  model {mae:6.1f} cm  persistence {base:6.1f} cm  "
                      f"{'✓' if mae < base else '✗'}  (train {int(tr.sum())}, test {int(te.sum())})", flush=True)
    (ART / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


def score(conn, now: datetime | None = None) -> int:
    """Predict from the latest hour for each modelled station; merge into station_risk.forecast_json."""
    import lightgbm as lgb
    rep_path = ART / "report.json"
    if not rep_path.exists():
        return 0
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    now = now or datetime.now().astimezone()
    up = _edges(conn)
    n = 0
    for sid_s, rep in report.items():
        sid = int(sid_s)
        ups = rep["upstream"]
        raw = {s: load_hourly(conn, s, now - timedelta(hours=30)) for s in [sid] + ups}
        if not raw[sid]:
            continue
        last = max(raw[sid])
        if now - last > timedelta(hours=3):
            continue
        hours = [last - timedelta(hours=k) for k in range(25, -1, -1)]
        data = {s: _arrays(r, hours) for s, r in raw.items()}
        X = features(data, sid, ups, np.array([len(hours) - 1]))
        level, diff = data[sid][-1, 0], data[sid][-1, 1]
        out = {}
        for h in HORIZONS:
            r = rep.get(str(h)) or {}
            path = ART / f"{sid}_{h}h.txt"
            if not r.get("use") or not path.exists():
                continue
            delta = float(lgb.Booster(model_file=str(path)).predict(X)[0])
            out[str(h)] = {"level": round(level + delta, 2), "mae_cm": r["mae_cm"],
                           "diff_wl_bank": None if np.isnan(diff) else round(diff + delta, 2)}
        if out:
            conn.execute("UPDATE station_risk SET forecast_json = forecast_json || jsonb_build_object('model', %s::jsonb) "
                         "WHERE station_id = %s", (json.dumps({"from": last.isoformat(), "horizons": out}), sid))
            # Model sees the bank being crossed within 12 h → at least 'warning' (§8: < 6 h to bank is warning;
            # the model only speaks for horizons where it beat persistence).
            if any((out.get(h) or {}).get("diff_wl_bank") is not None and out[h]["diff_wl_bank"] >= 0 for h in ("6", "12")):
                conn.execute("UPDATE station_risk SET risk_tier = 'warning', forecast_json = forecast_json || "
                             "'{\"tier_reason\": \"model_over_bank_12h\"}'::jsonb "
                             "WHERE station_id = %s AND risk_tier IN ('normal', 'watch')", (sid,))
            n += 1
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "train":
        train()
    else:
        with connect() as c:
            print(f"scored {score(c)} stations")
