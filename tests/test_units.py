"""Unit tests for the staged pipeline's building blocks. No warehouse needed."""

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from rca.forecast import Calendar, forecast_total
from rca.planner import SlicePlanner, Windows, predicate
from rca.semantic_client import MetricShape
from rca.shapley import AdditiveGame, attribute, owen_from_values, owen_terms
from rca.sketch import decode, estimate, merge, registers_of

P = 14


def _hashes(values: np.ndarray) -> np.ndarray:
    """splitmix64 finaliser: the tests need well-mixed 64-bit hashes, like DuckDB's hash()."""
    with np.errstate(over="ignore"):
        z = values.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return z ^ (z >> np.uint64(31))


# --------------------------------------------------------------------------- HLL
@pytest.mark.parametrize("n", [100, 5_000, 30_000, 200_000])
def test_hll_estimate_accuracy(n):
    regs = registers_of(_hashes(np.arange(n)), P)
    assert estimate(regs, P) == pytest.approx(n, rel=0.03)


def test_hll_merge_is_union():
    pool = _hashes(np.arange(60_000))
    a, b = pool[:40_000], pool[20_000:]  # overlap of 20k
    merged = merge(np.stack([registers_of(a, P), registers_of(b, P)]))
    assert estimate(merged, P) == pytest.approx(60_000, rel=0.03)


def test_hll_decode_roundtrip():
    regs = np.zeros(1 << P, np.uint8)
    regs[[3, 77, 16383]] = [5, 2, 9]
    encoded = ",".join(f"{i}:{regs[i]}" for i in np.nonzero(regs)[0])
    assert np.array_equal(decode(encoded, P), regs)


# --------------------------------------------------------------------------- Owen values
def _game(cells, flags, kind="ratio"):
    df = pd.DataFrame(cells, columns=["n1", "d1", "nh", "dh"])
    return AdditiveGame(kind, np.array(flags, bool), df.n1, df.d1, df.nh, df.dh, per=7)


def test_owen_is_efficient():
    rng = np.random.default_rng(1)
    flags = rng.random((16, 3)) < 0.5
    cells = np.column_stack([rng.uniform(10, 50, 16), rng.uniform(500, 900, 16),
                             rng.uniform(10, 50, 16), rng.uniform(500, 900, 16)])
    g = _game(cells, flags)
    unions = [[0, 1], [2, 3], [4, 5]]
    C, terms = owen_terms(unions, 6)
    phi = owen_from_values(g.value(C), terms, 6)
    everyone = np.ones((1, 6), bool)
    assert phi.sum() == pytest.approx(g.value(everyone)[0])


# Cells: (n1, d1, nh, dh). Columns of flags: A = the real cause, B = loose superset of A,
# M = a big "mobile-like" segment overlapping most of A, E = exact equivalent of A.
BROKEN = (5, 1000, 50, 1000)  # rate collapsed 50 -> 5 per 1000
NORMAL = (50, 1000, 50, 1000)


def test_loose_superset_and_big_overlap_are_rejected():
    cells = [BROKEN, BROKEN, NORMAL, NORMAL, NORMAL, NORMAL]
    #        A  B  M
    flags = [[1, 1, 1],   # broken, mobile
             [1, 1, 0],   # broken, not mobile
             [0, 1, 1],   # rest of B, mobile
             [0, 0, 1],
             [0, 0, 1],
             [0, 0, 0]]
    att = attribute(_game(cells, flags), ["A", "B", "M"], n_boot=0)
    status = {c.label: c.status for c in att.candidates}
    assert status == {"A": "accepted", "B": "rejected", "M": "rejected"}
    assert att.accepted[0].phi == pytest.approx(att.anomaly)


def test_slice_of_homogeneous_segment_gives_way():
    cells = [BROKEN, BROKEN, BROKEN, NORMAL]
    flags = [[1, 1], [0, 1], [0, 1], [0, 0]]  # S is a third of G; G is uniformly broken
    att = attribute(_game(cells, flags), ["S", "G"], n_boot=0)
    assert [c.label for c in att.accepted] == ["G"]


def test_equivalent_segments_merge():
    cells = [BROKEN, NORMAL, NORMAL]
    flags = [[1, 1], [0, 0], [0, 0]]
    att = attribute(_game(cells, flags), ["country=US", "region=NA"], n_boot=0)
    assert len(att.candidates) == 1 and att.candidates[0].segments == ["country=US", "region=NA"]


def test_mix_shift_is_credited_to_volume():
    # High-converting segment loses half its traffic; its rate is unchanged.
    cells = [(50, 500, 100, 1000), (180, 9000, 180, 9000)]
    att = attribute(_game(cells, [[1], [0]]), ["email"], n_boot=0)
    c = att.accepted[0]
    assert c.driver() == "mix"
    assert c.phi_by_effect["rate"] == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------- forecast
def test_forecast_learns_holiday_and_weekday():
    days = pd.date_range("2025-09-01", "2026-10-08", freq="D")
    rng = np.random.default_rng(3)
    holiday_days = [dt.date(2025, 10, 3), dt.date(2026, 10, 3)]
    y = 1000 * np.where(days.dayofweek >= 5, 0.8, 1.0) * rng.normal(1, 0.02, len(days))
    y[np.isin(days.date, holiday_days)] *= 0.5
    cal = Calendar(pd.DataFrame({"date": holiday_days, "scope": "DE", "holiday": "unity_day"}))
    target = (dt.date(2026, 10, 2), dt.date(2026, 10, 8))
    fc = forecast_total(days, y, target, cal)
    actual = y[(days.date >= target[0]) & (days.date <= target[1])].sum()
    assert fc.expected_total == pytest.approx(actual, rel=0.03)
    naive = forecast_total(days, y, target, Calendar())
    assert abs(naive.expected_total - actual) > abs(fc.expected_total - actual)


# --------------------------------------------------------------------------- planner
def _planner(**kw):
    sl = SimpleNamespace(dialect="duckdb")
    shape = MetricShape("conversion_rate", "Conversion", "ratio", "orders", "sessions")
    w = Windows.ending(dt.date(2026, 10, 8), 7, 28, weekly=False)
    return SlicePlanner(sl, shape, w, **kw)


def test_pack_respects_set_and_cell_budgets():
    pl = _planner(max_sets=3, cell_budget=35 * 1000)
    pl.cardinality = {"a": 10, "b": 10, "c": 10, "d": 1000}
    blocks = pl._pack([frozenset({x}) for x in "abc"] + [frozenset({"a", "b"}), frozenset({"d"})])
    assert all(len(b.sets) <= 3 for b in blocks)
    assert all(pl._est_cells(b.dims) <= 35 * 1000 for b in blocks if len(b.sets) > 1)


def test_split_routes_rows_by_grouping_bitmask():
    pl = _planner()
    block = SimpleNamespace(dims=["a", "b"], sets=[frozenset({"a"}), frozenset({"a", "b"})])
    raw = pd.DataFrame(
        [  # rca_win, g0, g1, a, b, n, d
            (0, 0, 1, "x", None, 10, 100), (1, 0, 1, "x", None, 40, 400),
            (0, 0, 0, "x", "y", 3, 30), (1, 0, 0, "x", "y", 9, 90),
        ],
        columns=["rca_win", "rca_g0", "rca_g1", "a", "b", "n", "d"],
    )
    out = pl._split(raw, block)
    a = out[frozenset({"a"})].iloc[0]
    assert (a.n0, a.n1, a.d0, a.d1) == (40, 10, 400, 100)
    ab = out[frozenset({"a", "b"})].iloc[0]
    assert (ab.n0, ab.n1) == (9, 3)


def test_weekly_windows_need_whole_weeks():
    with pytest.raises(ValueError):
        Windows.ending(dt.date(2026, 10, 8), 5, 28, weekly=True)


def test_predicate_escapes_quotes():
    assert predicate((("session__country", "O'Land"),)) == "(session__country = 'O''Land')"
