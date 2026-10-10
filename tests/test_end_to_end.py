"""End-to-end: run the engine through MetricFlow and score it against the planted ground truth.

Requires the warehouse: python scripts/generate_data.py && dbt build --profiles-dir .
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT / "rca.duckdb").exists() or not (ROOT / "target" / "semantic_manifest.json").exists(),
    reason="warehouse not built",
)


@pytest.fixture(scope="module")
def sl():
    from rca.semantic_client import SemanticLayer

    return SemanticLayer(ROOT)


@pytest.fixture(scope="module")
def truth():
    return json.loads((ROOT / "data" / "ground_truth.json").read_text())


def _segments(result):
    from rca.semantic_client import pretty_dimension

    return [{pretty_dimension(k): v for k, v in c.dims} for c in result.causes]


def test_discovers_joined_dimensions(sl):
    dims = sl.categorical_dimensions("conversion_rate")
    assert "customer__customer_tier" in dims  # only reachable via the customer entity join
    assert "session__platform" in dims


def test_ratio_metric_resolved_from_manifest(sl):
    shape = sl.metric_shape("conversion_rate")
    assert (shape.kind, shape.numerator, shape.denominator) == ("ratio", "orders", "sessions")


@pytest.mark.parametrize("metric", ["conversion_rate", "revenue_per_session", "revenue", "orders"])
def test_recovers_planted_causes(sl, truth, metric):
    from rca.engine import run_rca

    result = run_rca(sl, metric)
    found = _segments(result)
    expected = [c["segment"] for c in truth["causes"]]
    canon = lambda segs: sorted(str(sorted(s.items())) for s in segs)  # noqa: E731
    assert canon(found) == canon(expected), found
    assert str(result.detection.current[0]) == truth["incident_window"][0]
    assert result.residual_p > 0.05


def test_rate_vs_mix_labels(sl, truth):
    from rca.engine import run_rca

    result = run_rca(sl, "conversion_rate")
    labels = {str(sorted(_segments_one(c).items())): c.driver for c in result.causes}
    for cause in truth["causes"]:
        assert labels[str(sorted(cause["segment"].items()))] == cause["type"]


def _segments_one(c):
    from rca.semantic_client import pretty_dimension

    return {pretty_dimension(k): v for k, v in c.dims}


def test_quiet_period_reports_no_cause(sl):
    """Before the incident nothing happened. The engine should say so, not invent a story."""
    import datetime as dt

    from rca.engine import run_rca

    result = run_rca(sl, "conversion_rate", current_end=dt.date(2026, 9, 24))
    assert result.causes == []
