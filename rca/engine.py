"""Orchestration: detect the anomaly, crawl the semantic graph, attribute, explain."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from rca.attribution import (
    Cause,
    DimensionScore,
    best_candidate,
    decompose,
    deseasonalize,
    pairs,
    rank_dimensions,
    welch_p,
    window_totals,
)
from rca.semantic_client import TIME_GROUP_BY, MetricShape, SemanticLayer, segment_filter


@dataclass
class Detection:
    baseline: tuple[dt.date, dt.date]
    current: tuple[dt.date, dt.date]
    baseline_value: float
    current_value: float
    baseline_daily_std: float
    z_score: float
    daily: pd.DataFrame

    @property
    def pct_change(self) -> float:
        return self.current_value / self.baseline_value - 1 if self.baseline_value else float("nan")

    @property
    def is_anomalous(self) -> bool:
        return abs(self.z_score) >= 3

    @property
    def is_proportion(self) -> bool:
        """Ratio metrics whose numerator never exceeds the denominator (rates, not averages)."""
        cols = [c for c in self.daily.columns if c not in ("metric_time__day", "value")]
        return len(cols) == 2 and bool((self.daily[cols[0]] <= self.daily[cols[1]]).all())


@dataclass
class RCAResult:
    metric: MetricShape
    detection: Detection
    dimensions: list[str]
    dimension_ranking: list[DimensionScore]
    one_way: dict[str, pd.DataFrame]
    two_way: dict[tuple[str, str], pd.DataFrame]
    causes: list[Cause]
    queries_run: int
    seconds: float
    sample_sql: str = ""
    notes: list[str] = field(default_factory=list)
    residual_pct: float = float("nan")
    residual_p: float = float("nan")
    rounds: list[dict] = field(default_factory=list)

    @property
    def explained(self) -> float:
        return sum(c.pct_of_change for c in self.causes)


def _value(df: pd.DataFrame, shape: MetricShape) -> pd.Series:
    if shape.kind == "ratio":
        return df[shape.numerator] / df[shape.denominator]
    return df[shape.numerator]


def detect(
    sl: SemanticLayer,
    shape: MetricShape,
    current_days: int = 7,
    baseline_days: int = 28,
    current_end: Optional[dt.date] = None,
) -> Detection:
    """Compare the latest ``current_days`` against the preceding ``baseline_days``.

    Using whole weeks for both windows cancels day-of-week seasonality.
    """
    daily = sl.query(shape.base_metrics).sort_values(TIME_GROUP_BY).reset_index(drop=True)
    end = current_end or daily[TIME_GROUP_BY].max()
    cur = (end - dt.timedelta(days=current_days - 1), end)
    base = (cur[0] - dt.timedelta(days=baseline_days), cur[0] - dt.timedelta(days=1))
    daily["value"] = _value(daily, shape)

    def window_value(lo, hi):
        w = daily[daily[TIME_GROUP_BY].between(lo, hi)]
        if shape.kind == "ratio":
            return w[shape.numerator].sum() / w[shape.denominator].sum()
        return w[shape.numerator].sum() / len(w)

    b_daily = daily[daily[TIME_GROUP_BY].between(*base)]["value"]
    b_val, c_val = window_value(*base), window_value(*cur)
    std = float(b_daily.std(ddof=1))
    z = (c_val - b_val) / (std / np.sqrt(current_days)) if std > 0 else float("inf")
    return Detection(base, cur, float(b_val), float(c_val), std, float(z), daily)


def run_rca(
    sl: SemanticLayer,
    metric: str,
    current_days: int = 7,
    baseline_days: int = 28,
    current_end: Optional[dt.date] = None,
    depth: int = 2,
    max_causes: int = 4,
    alpha: float = 0.01,
    min_pct: float = 0.10,
) -> RCAResult:
    """Iteratively peel off root causes until what is left is indistinguishable from noise.

    Round k analyses the population *excluding* causes 1..k-1 (pushed down to the warehouse as
    MetricFlow where-filters), so a dominant cause cannot smear into every other dimension.
    """
    t0 = time.perf_counter()
    sl.queries_run = 0
    sl.sql_log.clear()
    shape = sl.metric_shape(metric)
    det = detect(sl, shape, current_days, baseline_days, current_end)
    dims = sl.categorical_dimensions(metric)
    window = (det.baseline[0], det.current[1])

    def fetch(keys: list[str], where: list[str]) -> pd.DataFrame:
        return sl.query(shape.base_metrics, group_by=keys, start=window[0], end=window[1], where=where)

    def slice_by(keys: list[str], where: list[str]) -> pd.DataFrame:
        totals = window_totals(fetch(keys, where), keys, shape.numerator, shape.denominator, det.baseline, det.current)
        return decompose(totals, keys, shape.kind, baseline_days, current_days)

    def change_p(where: list[str], component: str = "value") -> float:
        """Weekday-adjusted Welch test on a population's daily value, numerator or denominator."""
        daily = fetch([], where)
        in_base = daily[TIME_GROUP_BY].between(*det.baseline)
        in_cur = daily[TIME_GROUP_BY].between(*det.current)
        series = {
            "value": lambda: _value(daily, shape),
            "numerator": lambda: daily[shape.numerator],
            "denominator": lambda: daily[shape.denominator],
        }[component]()
        value = deseasonalize(series, daily[TIME_GROUP_BY], in_base)
        return welch_p(value[in_base], value[in_cur])

    def evidence_p(cause: Cause, where: list[str]) -> float:
        """Is the segment's own movement real? Rate causes: test its value; mix causes: its traffic."""
        component = "denominator" if cause.driver == "mix" else "value"
        return change_p(where + [segment_filter(cause.dims)], component)

    causes: list[Cause] = []
    rounds: list[dict] = []
    first_one_way: dict[str, pd.DataFrame] = {}
    first_two_way: dict[tuple[str, str], pd.DataFrame] = {}
    sample_sql = ""
    exclude: list[str] = []
    for k in range(max_causes):
        population_p = change_p(exclude)
        n_before = len(sl.sql_log)
        one_way = {d: slice_by([d], exclude) for d in dims}
        two_way = {pr: slice_by(list(pr), exclude) for pr in pairs(dims)} if depth >= 2 else {}
        if k == 0:
            first_one_way, first_two_way = one_way, two_way
        best = best_candidate(one_way, two_way, shape.kind, min_pct=min_pct)
        trace = {"round": k + 1, "population_p": population_p, "picked": None, "evidence_p": None}
        rounds.append(trace)
        if best is None:
            break
        best.evidence_p = evidence_p(best, exclude)
        trace.update(picked=best.dims, evidence_p=best.evidence_p, accepted=best.evidence_p < alpha)
        if not trace["accepted"]:
            break
        if not sample_sql:
            keys = [d for d, _ in best.dims]
            order = [[d] for d in one_way] + [list(pr) for pr in two_way]
            sample_sql = sl.sql_log[n_before + order.index(sorted(keys) if len(keys) > 1 else keys)]
        causes.append(best)
        exclude = exclude + [f"NOT {segment_filter(best.dims)}"]

    residual = _exclusive_attribution(sl, shape, causes, fetch, det, baseline_days, current_days)
    final_p = change_p(exclude) if causes else float("nan")

    notes = []
    if not det.is_anomalous:
        notes.append(f"|z| = {abs(det.z_score):.1f} < 3: the change may be noise. Attribution is shown anyway.")
    return RCAResult(
        metric=shape,
        detection=det,
        dimensions=dims,
        dimension_ranking=rank_dimensions(first_one_way),
        one_way=first_one_way,
        two_way=first_two_way,
        causes=causes,
        queries_run=sl.queries_run,
        seconds=time.perf_counter() - t0,
        sample_sql=sample_sql,
        notes=notes,
        residual_pct=residual,
        residual_p=final_p,
        rounds=rounds,
    )


def _exclusive_attribution(sl, shape, causes, fetch, det, baseline_days, current_days) -> float:
    """Re-attribute over disjoint segments (C1, C2 minus C1, ..., rest) so percentages add to 100%.

    Overwrites each cause's pct/rate/mix with its exclusive share; returns the residual share.
    """
    if not causes:
        return 1.0
    rows, prior = [], []
    for i, c in enumerate(causes):
        where = prior + [segment_filter(c.dims)]
        rows.append((f"cause_{i}", where))
        prior = prior + [f"NOT {segment_filter(c.dims)}"]
    rows.append(("rest", prior))

    frames = []
    for name, where in rows:
        daily = fetch([], where)
        daily["segment"] = name
        frames.append(daily)
    totals = window_totals(
        pd.concat(frames), ["segment"], shape.numerator, shape.denominator, det.baseline, det.current
    )
    parts = decompose(totals, ["segment"], shape.kind, baseline_days, current_days).set_index("segment")
    for i, c in enumerate(causes):
        r = parts.loc[f"cause_{i}"]
        c.pct_of_change = float(r["pct_of_change"])
        c.contribution = float(r["contribution"])
        if shape.kind == "ratio":
            c.rate_effect, c.mix_effect = float(r["rate_effect"]), float(r["mix_effect"])
    return float(parts.loc["rest", "pct_of_change"]) if "rest" in parts.index else 0.0
