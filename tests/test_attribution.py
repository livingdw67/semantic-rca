"""Unit tests for the attribution math. No warehouse needed."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from rca.attribution import best_candidate, decompose, deseasonalize, refine, _row_to_cause
from rca.semantic_client import segment_filter


def _ratio_frame(rng, k=6):
    d0 = rng.integers(500, 5000, k).astype(float)
    d1 = rng.integers(500, 5000, k).astype(float)
    n0 = np.floor(d0 * rng.uniform(0.01, 0.1, k))
    n1 = np.floor(d1 * rng.uniform(0.01, 0.1, k))
    return pd.DataFrame({"seg": [f"s{i}" for i in range(k)], "n0": n0, "n1": n1, "d0": d0, "d1": d1})


@pytest.mark.parametrize("seed", range(5))
def test_ratio_decomposition_is_exact(seed):
    df = _ratio_frame(np.random.default_rng(seed))
    out = decompose(df, ["seg"], "ratio", base_days=28, cur_days=7)
    R0 = df.n0.sum() / df.d0.sum()
    R1 = df.n1.sum() / df.d1.sum()
    assert out.contribution.sum() == pytest.approx(R1 - R0, abs=1e-12)
    assert (out.rate_effect + out.mix_effect).sum() == pytest.approx(R1 - R0, abs=1e-12)
    assert out.pct_of_change.sum() == pytest.approx(1.0)


def test_ratio_decomposition_handles_new_and_vanished_segments():
    df = pd.DataFrame(
        {"seg": ["a", "b", "new", "gone"], "n0": [10, 20, 0, 5], "d0": [100, 400, 0, 50],
         "n1": [9, 25, 3, 0], "d1": [100, 400, 30, 0]}
    )
    out = decompose(df, ["seg"], "ratio", 7, 7)
    R0 = df.n0.sum() / df.d0.sum()
    R1 = df.n1.sum() / df.d1.sum()
    assert np.isfinite(out.contribution).all()
    assert out.contribution.sum() == pytest.approx(R1 - R0)


def test_simple_decomposition_normalises_per_day():
    df = pd.DataFrame({"seg": ["a", "b"], "n0": [280.0, 140.0], "n1": [35.0, 35.0]})
    out = decompose(df, ["seg"], "simple", base_days=28, cur_days=7).set_index("seg")
    # a: 10/day -> 5/day, b: 5/day -> 5/day. All of the change is in a.
    assert out.loc["a", "contribution"] == pytest.approx(-5.0)
    assert out.loc["a", "pct_of_change"] == pytest.approx(1.0)
    assert out.loc["b", "pct_of_change"] == pytest.approx(0.0)


def test_mix_shift_is_labelled_mix_not_rate():
    # Segment "hi" converts at 10%, "lo" at 2%. Rates are unchanged; "hi" loses half its traffic.
    df = pd.DataFrame({"seg": ["hi", "lo"], "n0": [100, 180], "d0": [1000, 9000],
                       "n1": [50, 180], "d1": [500, 9000]})
    out = decompose(df, ["seg"], "ratio", 7, 7)
    hi = _row_to_cause(out.set_index("seg").loc["hi"].to_frame().T.assign(seg="hi").iloc[0], "ratio")
    assert hi.driver == "mix"
    assert abs(hi.rate_effect) < 1e-12


def _two_way_tables():
    """platform x country and platform x device, where iOS-DE breaks and device is a red herring."""
    rows = []
    for platform in ["ios", "android"]:
        for country in ["DE", "US", "FR", "GB"]:
            for device in ["mobile", "tablet"]:
                d = 1000.0 * (0.85 if device == "mobile" else 0.15)
                r0 = 0.05
                r1 = 0.005 if (platform == "ios" and country == "DE") else 0.05
                rows.append(dict(platform=platform, country=country, device=device,
                                 n0=d * r0 * 4, d0=d * 4, n1=d * r1, d1=d))
    cube = pd.DataFrame(rows)

    def table(keys):
        t = cube.groupby(keys)[["n0", "d0", "n1", "d1"]].sum().reset_index()
        return decompose(t, keys, "ratio", 28, 7)

    one = {k: table([k]) for k in ["platform", "country", "device"]}
    two = {k: table(list(k)) for k in [("country", "platform"), ("device", "platform"), ("country", "device")]}
    return one, two


def test_refine_drills_into_concentrated_child_only():
    one, two = _two_way_tables()
    ios_row = one["platform"].set_index("platform").loc["ios"]
    ios = _row_to_cause(ios_row.to_frame().T.assign(platform="ios").iloc[0], "ratio")
    refined = refine(ios, "platform", "ios", two, "ratio")
    # Mobile is 85% of iOS and carries 85% of the drop -> not surprising. DE is the real story.
    assert dict(refined.dims) == {"platform": "ios", "country": "DE"}


def test_best_candidate_finds_planted_interaction():
    one, two = _two_way_tables()
    best = best_candidate(one, two, "ratio")
    assert dict(best.dims) == {"platform": "ios", "country": "DE"}
    assert best.driver == "rate"


def test_deseasonalize_removes_weekday_pattern():
    days = pd.Series(pd.date_range("2026-09-07", periods=28, freq="D").date)
    weekday = pd.to_datetime(days).dt.weekday
    values = pd.Series(np.where(weekday >= 5, 80.0, 100.0))
    adj = deseasonalize(values, days, pd.Series(True, index=days.index))
    assert adj.std() == pytest.approx(0.0, abs=1e-9)


def test_segment_filter_renders_metricflow_jinja_and_escapes_quotes():
    f = segment_filter([("session__platform", "ios"), ("session__country", "O'Land")])
    assert f == "({{ Dimension('session__platform') }} = 'ios' AND {{ Dimension('session__country') }} = 'O''Land')"
