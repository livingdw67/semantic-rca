"""Detection: is the current window anomalous, by how much, and what is the noise floor?

    naive change    = actual / baseline-window value - 1        (what the old engine reported)
    expected change = forecast / baseline-window value - 1      (seasonality, holidays, trend)
    anomaly         = actual / forecast - 1                     (what needs explaining)

The z-score uses the forecast's AR(1)-aware window sigma, so a 7-day window with autocorrelated
days is not treated as 7 independent draws.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from rca.attribution import deseasonalize
from rca.forecast import Calendar, bucket_design, changepoints, daily_design, fit_forecast, two_sided_p
from rca.planner import Windows
from rca.semantic_client import TIME_GROUP_BY, MetricShape

MIN_DAILY_HISTORY = 56
MIN_WEEKLY_HISTORY = 12


@dataclass
class Detection:
    windows: Windows
    method: str  # "forecast" or "naive"
    actual: float
    expected: float
    naive: float
    log_sigma: float
    z: float
    p: float
    history: pd.DataFrame  # period, actual, expected (forecast fit / prediction)
    is_proportion: bool
    level_per_unit: float  # metric level used to convert relative noise into contribution units

    @property
    def anomaly_pct(self) -> float:
        return self.actual / self.expected - 1

    @property
    def naive_pct(self) -> float:
        return self.actual / self.naive - 1 if self.naive else float("nan")

    @property
    def expected_pct(self) -> float:
        return self.expected / self.naive - 1 if self.naive else float("nan")

    @property
    def noise_floor(self) -> float:
        """Smallest relative change distinguishable from noise at 95%."""
        return math.exp(1.96 * self.log_sigma) - 1

    @property
    def is_anomalous(self) -> bool:
        return self.p < 0.01

    def contribution_se(self, share: np.ndarray | float) -> np.ndarray | float:
        """Approximate SE of a segment's contribution, in metric units, for a segment with this
        share of volume: relative noise of a share-s slice scales like 1/sqrt(s), its weight in the
        total like s, so SE ~ level * sigma * sqrt(s)."""
        return self.level_per_unit * self.log_sigma * np.sqrt(np.clip(share, 0, 1))


def _value(df: pd.DataFrame, shape: MetricShape) -> pd.Series:
    if shape.kind == "ratio":
        return df[shape.numerator] / df[shape.denominator].replace(0, np.nan)
    return df[shape.numerator]


def _window_value(df: pd.DataFrame, shape: MetricShape, per: int) -> float:
    if shape.kind == "ratio":
        return float(df[shape.numerator].sum() / df[shape.denominator].sum())
    return float(df[shape.numerator].sum() / per)


def detect(
    history: pd.DataFrame,
    shape: MetricShape,
    windows: Windows,
    calendar: Calendar,
    use_forecast: bool = True,
) -> Detection:
    if windows.weekly:
        return _detect_weekly(history, shape, windows, calendar, use_forecast)
    return _detect_daily(history, shape, windows, calendar, use_forecast)


def _detect_daily(history, shape, windows, calendar, use_forecast) -> Detection:
    df = history.copy()
    df[TIME_GROUP_BY] = pd.to_datetime(df[TIME_GROUP_BY])
    df = df.set_index(TIME_GROUP_BY).sort_index()
    df = df[df.index <= pd.Timestamp(windows.end)]
    days = pd.DatetimeIndex(df.index)
    cur_lo, cur_hi = (pd.Timestamp(d) for d in windows.current)
    base_lo, base_hi = (pd.Timestamp(d) for d in windows.baseline)
    target = np.asarray((days >= cur_lo) & (days <= cur_hi))
    train = np.asarray(days < cur_lo)
    in_base = np.asarray((days >= base_lo) & (days <= base_hi))
    h = int(target.sum())
    base_days, _ = windows.normalisers
    actual = _window_value(df[target], shape, h)
    naive = _window_value(df[in_base], shape, base_days)
    value = _value(df, shape)
    is_prop = shape.kind == "ratio" and bool((df[shape.numerator] <= df[shape.denominator]).all())

    if use_forecast and train.sum() >= MIN_DAILY_HISTORY:
        design = daily_design(days, calendar, days.min(), changepoints(days[train]), yearly=train.sum() >= 730)
        weights = df[shape.denominator].to_numpy(float) if shape.kind == "ratio" else None
        fc = fit_forecast(value, design, train, target, weights=weights)
        if shape.kind == "ratio":
            d = df[shape.denominator].to_numpy(float)[target]
            expected = float(np.sum(fc.expected * d) / np.sum(d))
        else:
            expected = float(np.mean(fc.expected))
        sigma = fc.window_sigma(h)
        hist = pd.DataFrame({"actual": value})
        hist["expected"] = np.nan
        hist.loc[fc.fitted.index, "expected"] = fc.fitted
        hist.loc[days[target], "expected"] = fc.expected
        method = "forecast"
    else:
        adj = deseasonalize(value, pd.Series(days.date, index=df.index), pd.Series(in_base, index=df.index))
        sigma = float(np.std(np.log(adj[in_base].astype(float)), ddof=1) / math.sqrt(h))
        expected = naive
        hist = pd.DataFrame({"actual": value, "expected": naive})
        method = "naive"

    z = math.log(actual / expected) / sigma if sigma > 0 else float("inf")
    return Detection(windows, method, actual, expected, naive, sigma, z, two_sided_p(z),
                     hist.reset_index(names="period"), is_prop, level_per_unit=expected)


def _detect_weekly(history, shape, windows, calendar, use_forecast) -> Detection:
    df = history.sort_values("rca_bucket", ascending=False).reset_index(drop=True)
    value = _value(df, shape)
    target = (df["rca_bucket"] == 0).to_numpy()
    in_base = df["rca_bucket"].between(1, windows.n_baseline).to_numpy()
    train = (df["rca_bucket"] >= 1).to_numpy()
    actual = _window_value(df[target], shape, 1)
    naive = _window_value(df[in_base], shape, windows.n_baseline)
    is_prop = shape.kind == "ratio" and bool((df[shape.numerator] <= df[shape.denominator]).all())

    if use_forecast and train.sum() >= MIN_WEEKLY_HISTORY:
        n_buckets = int(df["rca_bucket"].max()) + 1
        start = windows.end - dt.timedelta(days=7 * n_buckets - 1)
        days = pd.date_range(start, windows.end, freq="D")
        bucket_of_day = ((pd.Timestamp(windows.end) - days).days // 7).to_numpy()
        daily = daily_design(days, calendar, days.min(), changepoints(days[bucket_of_day >= 1], every_days=91))
        weekly = bucket_design(daily, bucket_of_day)
        weekly.X = weekly.X.loc[df["rca_bucket"].to_numpy()]
        weekly.X.index = pd.RangeIndex(len(df))
        fc = fit_forecast(value.reset_index(drop=True), weekly, train, target)
        expected = float(fc.expected[0])
        sigma = fc.window_sigma(1)
        hist = pd.DataFrame({"period": df["rca_start"], "actual": value})
        hist["expected"] = np.nan
        hist.loc[fc.fitted.index, "expected"] = fc.fitted.to_numpy()
        hist.loc[np.where(target)[0], "expected"] = expected
        method = "forecast"
    else:
        sigma = float(np.std(np.log(value[in_base].astype(float)), ddof=1))
        expected = naive
        hist = pd.DataFrame({"period": df["rca_start"], "actual": value, "expected": naive})
        method = "naive"
    z = math.log(actual / expected) / sigma if sigma > 0 else float("inf")
    return Detection(windows, method, actual, expected, naive, sigma, z, two_sided_p(z), hist, is_prop,
                     level_per_unit=expected)
