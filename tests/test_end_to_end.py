"""End-to-end: run the staged pipeline through MetricFlow and score it against the planted ground
truth. Requires the warehouse:

    python scripts/generate_data.py && dbt build --profiles-dir .
"""

import dataclasses
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / "rca.duckdb").exists() or not (ROOT / "target" / "semantic_manifest.json").exists(),
    reason="warehouse not built",
)
QUIET_END = dt.date(2026, 9, 24)


@pytest.fixture(scope="module")
def sl():
    from rca.semantic_client import SemanticLayer

    return SemanticLayer(ROOT)


@pytest.fixture(scope="module")
def truth():
    return json.loads((ROOT / "data" / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def runs(sl):
    from rca.pipeline import run_rca

    cache = {}

    def get(metric, **kw):
        key = (metric, tuple(sorted(kw.items())))
        if key not in cache:
            cache[key] = run_rca(sl, metric, **kw)
        return cache[key]

    return get


def segments(label: str) -> list[dict]:
    """'country=US & platform=ios = platform=ios & region=NA (excluding ..)' -> both descriptions."""
    label = label.split(" (excluding ")[0]
    return [dict(part.split("=", 1) for part in desc.split(" & ")) for desc in label.split(" = ")]


def canon(seg: dict) -> str:
    return str(sorted(seg.items()))


def described(causes) -> dict[str, object]:
    """Every description of every cause: region=NA and country=US select the same rows here, and
    which one the crawl reaches first is not part of the contract."""
    return {canon(seg): c for c in causes for seg in segments(c.label)}


def found(result) -> dict[str, object]:
    return described(result.attribution.accepted)


# --------------------------------------------------------------------------- planner
@pytest.mark.parametrize("metric", ["conversion_rate", "buyer_rate"])
def test_planner_matches_metricflow(sl, metric):
    """GROUPING SETS over MetricFlow's compiled SQL == MetricFlow queried directly."""
    import pandas as pd

    from rca.attribution import window_totals
    from rca.planner import SlicePlanner, Windows

    shape = sl.metric_shape(metric)
    w = Windows.ending(dt.date(2026, 10, 8), 7, 28, weekly=shape.has_distinct)
    sets = [frozenset({"session__platform"}), frozenset({"session__platform", "session__country"}),
            frozenset({"customer__customer_tier"})]
    got = SlicePlanner(sl, shape, w).fetch(sets)
    for s in sets:
        keys = sorted(s)
        if shape.has_distinct:
            parts = []
            for b in range(w.n_baseline + 1):
                hi = w.end - dt.timedelta(days=7 * b)
                df = sl.query(shape.base_metrics, group_by=keys, start=hi - dt.timedelta(days=6), end=hi, by_day=False)
                parts.append(df.assign(b=b))
            df = pd.concat(parts)
            names = {shape.numerator: "n", shape.denominator: "d"}
            ref = (df[df.b > 0].groupby(keys)[shape.base_metrics].sum().rename(columns=lambda c: names[c] + "0")
                   .join(df[df.b == 0].groupby(keys)[shape.base_metrics].sum().rename(columns=lambda c: names[c] + "1"))
                   .reset_index())
        else:
            daily = sl.query(shape.base_metrics, group_by=keys, start=w.start, end=w.end)
            ref = window_totals(daily, keys, shape.numerator, shape.denominator, w.baseline, w.current)
        mine = got[s].sort_values(keys).reset_index(drop=True)
        ref = ref.sort_values(keys).reset_index(drop=True)
        for col in ("n0", "n1", "d0", "d1"):
            assert np.allclose(mine[col], ref[col]), (s, col)


# --------------------------------------------------------------------------- recall + precision
@pytest.mark.parametrize("metric", ["conversion_rate", "orders", "revenue_per_session", "buyer_rate"])
def test_recovers_all_planted_causes(runs, truth, metric):
    result = runs(metric)
    by_segment = found(result)
    for cause in truth["causes"]:
        assert canon(cause["segment"]) in by_segment, [c.label for c in result.attribution.accepted]
    assert len(result.attribution.accepted) == len(truth["causes"]), [c.label for c in result.attribution.accepted]


def test_revenue_recovers_the_three_strong_causes(runs, truth):
    """Raw revenue is heavy-tailed: the offsetting iOS-US effect (about -13% of the anomaly) does
    not clear the multiple-testing bar there, and must be reported as considered, not dropped."""
    result = runs("revenue")
    accepted = set(found(result))
    strong = {canon(c["segment"]) for c in truth["causes"] if c["direction"] == "down"}
    assert strong <= accepted
    assert accepted <= strong | {canon({"platform": "ios", "country": "US"})}
    considered = described(result.attribution.candidates)
    assert canon({"platform": "ios", "country": "US"}) in considered


def test_mechanisms_and_directions(runs, truth):
    result = runs("conversion_rate")
    by_seg = found(result)
    for cause in truth["causes"]:
        c = by_seg[canon(cause["segment"])]
        assert c.driver() == cause["type"], (c.label, c.phi_by_effect)
        explains_drop = c.phi / result.attribution.anomaly > 0
        assert explains_drop == (cause["direction"] == "down"), c.label


def test_equivalent_dimensions_are_reported_not_suppressed(runs):
    result = runs("conversion_rate")
    assert result.crawl.aliases["session__country"] == ["session__storefront"]
    us = found(result)[canon({"platform": "ios", "country": "US"})]
    assert "region=NA" in us.label  # region=NA describes the same rows as country=US here


def test_screening(runs):
    screened = runs("conversion_rate").crawl.screened
    assert set(screened) == {"session__landing_page_id", "session__is_test_traffic", "session__sample_bucket",
                             "session__checkout_status"}
    assert screened["session__checkout_status"].startswith("preflight: looks derived from the outcome")


def test_preflight_notes_reach_the_report(runs):
    from rca.report import to_markdown, to_summary

    result = runs("conversion_rate")
    assert any(n.startswith("`checkout_status` excluded") for n in result.preflight)
    assert "Pre-flight data checks" in to_markdown(result)
    assert to_summary(result)["preflight"] == result.preflight


def test_quiet_week_reports_nothing(runs):
    result = runs("conversion_rate", current_end=QUIET_END)
    assert not result.detection.is_anomalous
    assert result.attribution.accepted == []


# --------------------------------------------------------------------------- budget + baselines
@pytest.mark.parametrize("metric", ["conversion_rate", "orders", "revenue"])
def test_statement_budget(runs, metric):
    """28 dimensions, depth 3: the exhaustive engine needs ~500 queries per round."""
    assert runs(metric).queries_run <= 12


def test_forecast_sees_payday_in_the_window(runs):
    det = runs("conversion_rate").detection
    assert det.method == "forecast"
    assert det.expected_pct > 0.01  # Oct 2-3 are paydays
    assert det.anomaly_pct < det.naive_pct  # so the naive comparison understates the incident


def test_screen_sample_mode_finds_the_same_causes(sl, truth):
    from rca.crawl import CrawlConfig
    from rca.pipeline import PipelineConfig, run_rca

    cfg = dataclasses.replace(PipelineConfig(), crawl=dataclasses.replace(CrawlConfig(), screen_sample=0.2))
    result = run_rca(sl, "conversion_rate", config=cfg)
    assert {canon(c["segment"]) for c in truth["causes"]} <= set(found(result))
    assert len(result.attribution.accepted) == len(truth["causes"])
    assert any(s["purpose"] == "stage 2: sample screen" for s in result.statements)


@pytest.mark.slow
def test_exhaustive_oracle_runs(sl):
    from rca.exhaustive import run_rca as run_exhaustive

    result = run_exhaustive(sl, "conversion_rate", depth=2, max_causes=2)
    assert result.queries_run > 100
