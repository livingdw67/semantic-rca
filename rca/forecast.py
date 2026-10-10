"""Robust seasonal forecasting for baselines and noise floors.

Why not Prophet or Holt-Winters?

* Holt-Winters handles one seasonal period and has no holiday regressors; this data has weekly,
  monthly (payday) and holiday effects at once.
* Prophet's model is the right *shape* (additive trend + Fourier seasonality + holiday dummies)
  but pulls in Stan for a problem that is a penalised linear regression.

So this is Prophet's decomposition fitted as a Huber-robust ridge regression on log values, in
numpy. Robust loss matters: last year's incidents and outages are in the training data and must
not drag the baseline. Components:

    log y_t = trend (linear + ridge-penalised hinge changepoints every 8 weeks)
            + day of week
            + monthly: day-of-month Fourier terms + turn-of-month (days 1-3, last 3 days)
            + holidays: one dummy per (holiday, offset in -1..+1), from a calendar table
            + yearly Fourier terms, only with >= 2 years of history
            + AR(1) noise

The forecast's residual scale gives the **noise floor**: how big a change over the current
window must be before it is distinguishable from normal day-to-day variation.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

HOLIDAY_OFFSETS = (-1, 0, 1)


# --------------------------------------------------------------------------- calendar
@dataclass
class Calendar:
    """Holiday table: date, scope (country code, region, or ALL), holiday name."""

    holidays: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["date", "scope", "holiday"]))

    @classmethod
    def from_csv(cls, path: Path | str) -> "Calendar":
        p = Path(path)
        if not p.exists():
            return cls()
        df = pd.read_csv(p, parse_dates=["date"])
        df["date"] = df["date"].dt.date
        return cls(df)

    @property
    def names(self) -> list[str]:
        return sorted(self.holidays["holiday"].unique()) if len(self.holidays) else []

    def indicators(self, days: pd.DatetimeIndex) -> pd.DataFrame:
        cols = {}
        dates = pd.Series(days.date)
        for name in self.names:
            hdays = set(self.holidays.loc[self.holidays["holiday"] == name, "date"])
            for off in HOLIDAY_OFFSETS:
                shifted = {h + dt.timedelta(days=off) for h in hdays}
                cols[f"hol_{name}_{off:+d}"] = dates.isin(shifted).astype(float).to_numpy()
        return pd.DataFrame(cols, index=days)


# --------------------------------------------------------------------------- design
@dataclass
class Design:
    X: pd.DataFrame
    penalty: np.ndarray


def daily_design(
    days: pd.DatetimeIndex,
    calendar: Calendar,
    t0: pd.Timestamp,
    changepoints: list[pd.Timestamp],
    yearly: bool = False,
) -> Design:
    t = np.asarray((days - t0).days, dtype=float) / 365.25
    cols: dict[str, np.ndarray] = {"intercept": np.ones(len(days)), "trend": t}
    pen: dict[str, float] = {"intercept": 0.0, "trend": 0.0}
    for i, cp in enumerate(changepoints):
        cols[f"cp{i}"] = np.maximum(0.0, np.asarray((days - cp).days, dtype=float) / 365.25)
        pen[f"cp{i}"] = 20.0
    dow = days.dayofweek
    for k in range(1, 7):
        cols[f"dow{k}"] = (dow == k).astype(float)
        pen[f"dow{k}"] = 0.0
    dom = days.day.to_numpy()
    dim = days.days_in_month.to_numpy()
    phase = 2 * np.pi * (dom - 1) / dim
    for k in range(1, 5):
        cols[f"dom_sin{k}"], cols[f"dom_cos{k}"] = np.sin(k * phase), np.cos(k * phase)
        pen[f"dom_sin{k}"] = pen[f"dom_cos{k}"] = 1.0
    cols["tom_start"] = (dom <= 3).astype(float)
    cols["tom_end"] = (dom >= dim - 2).astype(float)
    pen["tom_start"] = pen["tom_end"] = 0.5
    if yearly:
        yphase = 2 * np.pi * days.dayofyear.to_numpy() / 365.25
        for k in range(1, 4):
            cols[f"yr_sin{k}"], cols[f"yr_cos{k}"] = np.sin(k * yphase), np.cos(k * yphase)
            pen[f"yr_sin{k}"] = pen[f"yr_cos{k}"] = 1.0
    hol = calendar.indicators(days)
    for c in hol:
        cols[c] = hol[c].to_numpy()
        pen[c] = 0.2
    X = pd.DataFrame(cols, index=days)
    return Design(X, np.array([pen[c] for c in X.columns]))


def bucket_design(daily: Design, buckets: np.ndarray) -> Design:
    """Weekly design = mean of the daily rows in each bucket. Day-of-week terms become constant
    (every week has one of each day) and are dropped."""
    X = daily.X.groupby(buckets).mean()
    keep = [c for c in X.columns if not c.startswith("dow")]
    keep = [c for c in keep if c == "intercept" or X[c].std() > 0]
    pen = dict(zip(daily.X.columns, daily.penalty))
    return Design(X[keep], np.array([pen[c] for c in keep]))


# --------------------------------------------------------------------------- fitting
def huber_ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, penalty: np.ndarray, delta: float = 1.345,
                iters: int = 40, protect: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """IRLS for Huber loss with a ridge penalty. Returns (beta, final robust weights).

    ``protect`` marks rows that keep full weight: holidays seen once look exactly like outliers
    to a robust loss, which would then refuse to learn the holiday effect it was given."""
    n = len(y)
    P = np.diag(penalty)
    beta = np.linalg.solve(X.T @ (X * w[:, None]) + P, X.T @ (w * y))
    hw = np.ones(n)
    for _ in range(iters):
        r = y - X @ beta
        scale = 1.4826 * np.median(np.abs(r - np.median(r))) or 1e-9
        u = np.abs(r) / scale
        hw = np.where(u <= delta, 1.0, delta / np.maximum(u, 1e-12))
        if protect is not None:
            hw = np.where(protect, 1.0, hw)
        W = w * hw
        new = np.linalg.solve(X.T @ (X * W[:, None]) + P, X.T @ (W * y))
        if np.max(np.abs(new - beta)) < 1e-8:
            beta = new
            break
        beta = new
    return beta, hw


@dataclass
class Forecast:
    """A fitted model plus its prediction for the target window."""

    expected: np.ndarray  # level-scale expectation per target period
    log_pred: np.ndarray
    sigma: float  # robust residual sd (log scale, per period)
    phi: float  # AR(1) coefficient of residuals
    fitted: pd.Series  # in-sample level-scale fit
    actual_train: pd.Series
    smear: float
    param_var: float  # variance of the window-mean prediction from parameter uncertainty

    def window_sigma(self, h: Optional[int] = None) -> float:
        """SD of log(window mean) over the h target periods: AR(1) noise + parameter error."""
        h = h or len(self.log_pred)
        idx = np.arange(h)
        corr = self.phi ** np.abs(idx[:, None] - idx[None, :])
        noise_var = self.sigma**2 * corr.sum() / h**2
        return float(math.sqrt(noise_var + self.param_var))


def fit_forecast(
    y: pd.Series,
    design: Design,
    train_mask: np.ndarray,
    target_mask: np.ndarray,
    weights: Optional[np.ndarray] = None,
    offset: float = 0.0,
) -> Forecast:
    """Fit on log(y + offset) over ``train_mask``; predict ``target_mask``."""
    X = design.X.to_numpy()
    ly = np.log(np.maximum(y.to_numpy(dtype=float) + offset, 1e-9))
    w = np.ones(len(y)) if weights is None else weights / np.mean(weights[train_mask])
    tm = train_mask & np.isfinite(ly)
    # Columns with no support in the training rows (e.g. a holiday never seen) carry no information.
    support = np.abs(X[tm]).sum(axis=0) > 0
    Xs = X[:, support]
    hol_cols = [i for i, c in enumerate(design.X.columns[support]) if c.startswith("hol_")]
    protect = (np.abs(Xs[tm][:, hol_cols]).sum(axis=1) > 0) if hol_cols else None
    beta, hw = huber_ridge(Xs[tm], ly[tm], w[tm], design.penalty[support], protect=protect)
    resid = ly[tm] - Xs[tm] @ beta
    sigma = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
    r0, r1 = resid[:-1], resid[1:]
    phi = float(np.clip(np.corrcoef(r0, r1)[0, 1], 0.0, 0.95)) if len(resid) > 3 else 0.0
    smear = float(np.mean(np.exp(np.clip(resid, -3 * sigma, 3 * sigma))))
    W = w[tm] * hw
    cov = np.linalg.pinv(Xs[tm].T @ (Xs[tm] * W[:, None]) + np.diag(design.penalty[support])) * sigma**2
    xbar = Xs[target_mask].mean(axis=0) if target_mask.any() else np.zeros(Xs.shape[1])
    log_pred = Xs[target_mask] @ beta
    fitted = pd.Series(np.exp(Xs[tm] @ beta) * smear - offset, index=y.index[tm])
    return Forecast(
        expected=np.exp(log_pred) * smear - offset,
        log_pred=log_pred,
        sigma=sigma,
        phi=phi,
        fitted=fitted,
        actual_train=y[tm],
        smear=smear,
        param_var=float(xbar @ cov @ xbar),
    )


def changepoints(train_days: pd.DatetimeIndex, every_days: int = 56, tail_days: int = 56) -> list[pd.Timestamp]:
    if len(train_days) == 0:
        return []
    start, end = train_days.min(), train_days.max() - pd.Timedelta(days=tail_days)
    out, cp = [], start + pd.Timedelta(days=every_days)
    while cp < end:
        out.append(cp)
        cp += pd.Timedelta(days=every_days)
    return out


# --------------------------------------------------------------------------- convenience
@dataclass
class SeriesForecast:
    """Expected total over the target days for one additive series (e.g. orders in a flag cell)."""

    expected_total: float
    log_sigma: float  # sd of log(expected total)
    modelled: bool
    daily: np.ndarray  # expected value for each target day


def forecast_total(
    days: pd.DatetimeIndex,
    values: np.ndarray,
    target: tuple[dt.date, dt.date],
    calendar: Calendar,
    min_daily: float = 5.0,
) -> Optional[SeriesForecast]:
    """Forecast the total of a daily count/sum series over ``target``. None if too sparse."""
    s = pd.Series(values, index=days, dtype=float)
    lo, hi = pd.Timestamp(target[0]), pd.Timestamp(target[1])
    target_mask = np.asarray((days >= lo) & (days <= hi))
    train_mask = np.asarray(days < lo)
    if train_mask.sum() < 56 or s[train_mask].mean() < min_daily:
        return None
    design = daily_design(days, calendar, days.min(), changepoints(days[train_mask]),
                          yearly=train_mask.sum() >= 730)
    fc = fit_forecast(s, design, train_mask, target_mask, offset=0.5)
    daily = np.maximum(fc.expected, 0.0)
    return SeriesForecast(float(daily.sum()), fc.window_sigma(), True, daily)


def two_sided_p(z: float) -> float:
    return float(2 * stats.norm.sf(abs(z)))
