import numpy as np

from model import auto_edges as ae

RNG = np.random.default_rng(0)


def flood_wave(n=2000):
    """A level series with random rises and falls, like a river through a wet season."""
    return np.cumsum(RNG.normal(0, 0.05, n))


def test_best_lag_finds_travel_time():
    up = flood_wave()
    down = np.roll(up, 10) - 5.0                 # same wave 10 h later, 5 m lower
    lag, c = ae.best_lag(up, down, range(0, 30))
    assert lag == 10 and c > 0.95


def test_judge_direction_from_height_and_lag():
    up = flood_wave() + 20
    down = np.roll(up, 8) - 3.0
    v = ae.judge(down, up)                       # order of the pair doesn't matter
    assert v["up"] == "b" and v["lag_h"] == 8


def test_judge_rejects_unrelated_flat_and_backwards():
    a = flood_wave() + 10
    assert ae.judge(a, flood_wave())["reason"] == "weak correlation"
    assert ae.judge(a, a + 0.05)["reason"] == "flat: can't tell direction"
    # The higher station's rises arrive later: water would be flowing uphill, so don't trust the pair.
    assert ae.judge(np.roll(a, 2) + 5, a)["reason"] == "lag points the other way"
    gaps = np.full(2000, np.nan)
    assert ae.judge(a, gaps)["reason"] == "no history"


def test_reduce_drops_shortcuts_only():
    edges = {(1, 2): {}, (2, 3): {}, (1, 3): {}, (3, 4): {}}
    assert ae.reduce(edges) == {(1, 3)}
