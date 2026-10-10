"""Attribution math. Pure pandas -- no warehouse access, so it is unit-testable.

Every function takes per-segment window totals with columns ``n0, n1`` (numerator in the
baseline/current window) and, for ratio metrics, ``d0, d1`` (denominator). Totals are
normalised to per-day averages before comparison so windows of different length are comparable.

Additive metrics (revenue, orders):
    contribution_i = n1_i - n0_i                       (sums exactly to the total change)

Ratio metrics (conversion rate = orders / sessions), with s = share of denominator, r = rate:
    R1 - R0 = sum_i  s1_i (r1_i - r0_i)  +  (s1_i - s0_i)(r0_i - R0)
                     \\_ rate effect _/    \\____ mix effect ____/
This identity is exact, so segment contributions always add up to the observed change and the
"% explained" numbers are not heuristics. Because the decomposition is linear, it coincides with
the Shapley attribution over segments.

Dimension ranking follows Adtributor (Bhagwan et al., NSDI '14): a dimension is surprising when
the distribution of the metric across its values shifts, measured by Jensen-Shannon divergence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from scipy import stats


def decompose(
    seg: pd.DataFrame,
    keys: list[str],
    kind: str,
    base_days: int,
    cur_days: int,
) -> pd.DataFrame:
    """Add contribution columns to per-segment totals. Returns a new frame sorted by impact."""
    df = seg.copy()
    for col in ("n0", "n1", "d0", "d1"):
        if col in df:
            df[col] = df[col].astype(float)
    df["n0_day"] = df["n0"] / base_days
    df["n1_day"] = df["n1"] / cur_days

    if kind == "simple":
        df["contribution"] = df["n1_day"] - df["n0_day"]
        df["rate_effect"] = np.nan
        df["mix_effect"] = np.nan
        df["share0"] = _safe_div(df["n0"], df["n0"].sum())
        df["share1"] = _safe_div(df["n1"], df["n1"].sum())
        df["value0"] = df["n0_day"]
        df["value1"] = df["n1_day"]
        total_change = df["contribution"].sum()
    elif kind == "ratio":
        R0 = df["n0"].sum() / df["d0"].sum()
        R1 = df["n1"].sum() / df["d1"].sum()
        s0 = df["d0"] / df["d0"].sum()
        s1 = df["d1"] / df["d1"].sum()
        # Rates for empty segments are irrelevant (their share is 0) -- pick values that keep
        # the identity exact and assign no spurious mix effect.
        r0 = pd.Series(np.where(df["d0"] > 0, _safe_div(df["n0"], df["d0"]), R0), index=df.index)
        r1 = pd.Series(np.where(df["d1"] > 0, _safe_div(df["n1"], df["d1"]), r0), index=df.index)
        df["share0"], df["share1"] = s0, s1
        df["value0"], df["value1"] = r0, r1
        df["rate_effect"] = s1 * (r1 - r0)
        df["mix_effect"] = (s1 - s0) * (r0 - R0)
        df["contribution"] = df["rate_effect"] + df["mix_effect"]
        df["volume_change"] = _safe_div(df["d1"] / cur_days, df["d0"] / base_days) - 1
        is_proportion = bool(((df["n0"] <= df["d0"]) & (df["n1"] <= df["d1"])).all())
        df["p_value"] = (
            [_two_prop_p(*row) for row in df[["n0", "d0", "n1", "d1"]].to_numpy()] if is_proportion else np.nan
        )
        total_change = R1 - R0
    else:
        raise ValueError(f"unknown metric kind {kind!r}")

    df["pct_of_change"] = df["contribution"] / total_change if total_change else 0.0
    df["surprise"] = _js_terms(df, kind)
    df["dims"] = [tuple(zip(keys, map(str, vals))) for vals in df[keys].itertuples(index=False)]
    return df.sort_values("pct_of_change", ascending=False).reset_index(drop=True)


def _safe_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(np.asarray(a, dtype=float), np.asarray(b, dtype=float))
    return np.where(np.isfinite(out), out, 0.0)


def _js_terms(df: pd.DataFrame, kind: str) -> pd.Series:
    """Per-element Jensen-Shannon contribution (Adtributor 'surprise')."""

    def js(p: pd.Series, q: pd.Series) -> pd.Series:
        m = (p + q) / 2
        with np.errstate(divide="ignore", invalid="ignore"):
            a = np.where(p > 0, p * np.log(p / m), 0.0)
            b = np.where(q > 0, q * np.log(q / m), 0.0)
        return pd.Series(0.5 * (a + b), index=p.index)

    p_n = df["n0"] / max(df["n0"].sum(), 1e-12)
    q_n = df["n1"] / max(df["n1"].sum(), 1e-12)
    out = js(p_n, q_n)
    if kind == "ratio":
        out = (out + js(df["share0"], df["share1"])) / 2
    return out


def _two_prop_p(n0: float, d0: float, n1: float, d1: float) -> float:
    """Two-sided two-proportion z-test that the rate changed inside a segment."""
    if d0 <= 0 or d1 <= 0:
        return float("nan")
    p_pool = (n0 + n1) / (d0 + d1)
    se = np.sqrt(p_pool * (1 - p_pool) * (1 / d0 + 1 / d1))
    if se == 0:
        return float("nan")
    z = (n1 / d1 - n0 / d0) / se
    return float(2 * stats.norm.sf(abs(z)))


# --------------------------------------------------------------------------- dimension ranking
@dataclass
class DimensionScore:
    dimension: str
    surprise: float
    top_value: str
    top_pct: float


def rank_dimensions(tables: dict[str, pd.DataFrame], min_ep: float = 0.10) -> list[DimensionScore]:
    """Rank dimensions by the surprise of the elements that explain >= ``min_ep`` of the change."""
    scores = []
    for dim, t in tables.items():
        explaining = t[t["pct_of_change"] >= min_ep]
        surprise = float(explaining["surprise"].sum()) if len(explaining) else 0.0
        top = t.iloc[0]
        scores.append(DimensionScore(dim, surprise, str(top[dim]), float(top["pct_of_change"])))
    return sorted(scores, key=lambda s: s.surprise, reverse=True)


# --------------------------------------------------------------------------- cause selection
@dataclass
class Cause:
    dims: tuple[tuple[str, str], ...]
    pct_of_change: float
    contribution: float
    value0: float
    value1: float
    share0: float
    share1: float
    rate_effect: float = float("nan")
    mix_effect: float = float("nan")
    volume_change: float = float("nan")
    p_value: float = float("nan")
    score: float = 0.0
    evidence_p: float = float("nan")
    refined_from: list[tuple[tuple[str, str], ...]] = field(default_factory=list)

    @property
    def driver(self) -> str:
        if np.isnan(self.rate_effect):
            return "level"
        return "rate" if abs(self.rate_effect) >= abs(self.mix_effect) else "mix"

    def label(self) -> str:
        return " AND ".join(f"{d}={v}" for d, v in self.dims)


def _row_to_cause(row: pd.Series, kind: str) -> Cause:
    get = lambda k: float(row[k]) if k in row and pd.notna(row[k]) else float("nan")  # noqa: E731
    c = Cause(
        dims=tuple(sorted(row["dims"])),
        pct_of_change=get("pct_of_change"),
        contribution=get("contribution"),
        value0=get("value0"),
        value1=get("value1"),
        share0=get("share0"),
        share1=get("share1"),
        rate_effect=get("rate_effect"),
        mix_effect=get("mix_effect"),
        volume_change=get("volume_change"),
        p_value=get("p_value"),
    )
    c.score = c.pct_of_change * (1 - _volume_share(c, kind))
    return c


def _volume_share(c: Cause, kind: str) -> float:
    """How big the segment is: share of traffic (ratio) or of the baseline metric (simple)."""
    return max(c.share0, c.share1) if kind == "ratio" else c.share0


def refine(
    parent: Cause,
    parent_dim: str,
    parent_value: str,
    two_way: dict[tuple[str, str], pd.DataFrame],
    kind: str,
    keep_ratio: float = 0.75,
    min_lift: float = 1.5,
) -> Cause:
    """Drill from ``A`` to ``A & B`` when the child is a *concentrated* explanation.

    The child must keep >= ``keep_ratio`` of the parent's contribution while being small enough
    that this is surprising: lift = (child pct / parent pct) / (child volume / parent volume).
    Without the lift test, "iOS" would be refined into "iOS & mobile" just because most iOS
    traffic is mobile.
    """
    best, best_lift = parent, 0.0
    parent_vol = _volume_share(parent, kind)
    for pair, t2 in two_way.items():
        if parent_dim not in pair:
            continue
        children = t2[t2[parent_dim].astype(str) == parent_value]
        for _, row in children.head(3).iterrows():
            child = _row_to_cause(row, kind)
            if parent.pct_of_change <= 0 or parent_vol <= 0:
                continue
            kept = child.pct_of_change / parent.pct_of_change
            lift = kept / max(_volume_share(child, kind) / parent_vol, 1e-9)
            if kept >= keep_ratio and lift >= min_lift and lift > best_lift:
                child.refined_from = [parent.dims]
                best, best_lift = child, lift
    return best


def best_candidate(
    one_way: dict[str, pd.DataFrame],
    two_way: dict[tuple[str, str], pd.DataFrame],
    kind: str,
    min_pct: float = 0.10,
) -> Optional[Cause]:
    """The single most compelling segment in this round, after drill-down.

    Candidates are single-dimension values explaining >= ``min_pct`` of the change, each refined
    to a two-dimension segment when that is sharper. They are scored by
    ``pct_of_change * (1 - segment share)`` so a big segment cannot win simply by being big.
    """
    candidates: dict[tuple, Cause] = {}
    for dim, t in one_way.items():
        for _, row in t[t["pct_of_change"] >= min_pct].iterrows():
            c = refine(_row_to_cause(row, kind), dim, str(row[dim]), two_way, kind)
            if c.dims in candidates:
                candidates[c.dims].refined_from.extend(c.refined_from)
            else:
                candidates[c.dims] = c
    if not candidates:
        return None
    return max(candidates.values(), key=lambda c: c.score)


def welch_p(base: pd.Series, cur: pd.Series) -> float:
    """Two-sided Welch t-test on daily values: is the remaining change distinguishable from noise?"""
    if len(base) < 2 or len(cur) < 2:
        return float("nan")
    return float(stats.ttest_ind(cur, base, equal_var=False).pvalue)


def deseasonalize(values: pd.Series, days: pd.Series, baseline_mask: pd.Series) -> pd.Series:
    """Divide out day-of-week factors estimated on the baseline window only.

    Weekend dips are not anomalies; leaving them in inflates daily variance and hides real shifts.
    """
    weekday = pd.to_datetime(days).dt.weekday
    base = values[baseline_mask]
    factors = base.groupby(weekday[baseline_mask]).mean() / base.mean()
    return values / weekday.map(factors).fillna(1.0).replace(0, 1.0)


def pairs(dims: Iterable[str]) -> list[tuple[str, str]]:
    return list(combinations(sorted(dims), 2))


def window_totals(
    daily: pd.DataFrame,
    keys: list[str],
    numerator: str,
    denominator: Optional[str],
    baseline: tuple,
    current: tuple,
    time_col: str = "metric_time__day",
) -> pd.DataFrame:
    """Collapse a daily, segmented frame into n0/n1(/d0/d1) per segment."""
    in_base = daily[time_col].between(*baseline)
    in_cur = daily[time_col].between(*current)
    cols = {numerator: "n"} | ({denominator: "d"} if denominator else {})
    agg0 = daily[in_base].groupby(keys)[list(cols)].sum().rename(columns=lambda c: cols[c] + "0")
    agg1 = daily[in_cur].groupby(keys)[list(cols)].sum().rename(columns=lambda c: cols[c] + "1")
    return agg0.join(agg1, how="outer").fillna(0.0).reset_index()
