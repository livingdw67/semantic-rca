"""Pre-flight data checks: catch the things that make an RCA confidently wrong.

Real semantic layers are messy. Three failure modes produce convincing-looking but false causes,
so they are checked before anything is attributed:

1. **Incomplete trailing days.** A late-arriving partition makes the last day look like a crash.
   Trailing days whose volume is far below the same weekday in previous weeks are excluded from
   the current window (and reported), unless the caller insists on keeping them.
2. **Renamed categories.** If "iOS" becomes "ios", one value fades out and another appears. That is
   a data change, not a cause; the dimension's history is broken, so it is excluded with a reason.
   Caught two ways: a value that vanished and one of similar size that appeared (a rename at the
   window boundary, any spelling), or two spellings of one label, one of them new or gone (a
   rename partway through a window).
3. **Outcome-derived dimensions.** A column like checkout_status (purchased / browsed) is defined
   by the outcome itself, so it "explains" every change perfectly and means nothing. For rate
   metrics: almost all volume sits in values whose rate is ~0% or ~100%. For level metrics: a
   low-cardinality dimension with values that never carry any of the metric.

Every exclusion can be overridden with ``config.meta.rca: include`` on the dimension, and any
dimension can be declared ``rca: outcome`` explicitly.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from rca.semantic_client import pretty_dimension


@dataclass
class Completeness:
    complete_end: dt.date
    excluded_days: list[dt.date] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)


def check_completeness(
    days: pd.Series,
    volume: pd.Series,
    holidays: Optional[set[dt.date]] = None,
    threshold: float = 0.6,
    max_days: int = 3,
) -> Completeness:
    """Trailing days whose volume is below ``threshold`` x the median of the same weekday over the
    previous four weeks. Holidays are reported but never trimmed: a quiet holiday is not missing data."""
    s = pd.Series(volume.to_numpy(float), index=pd.to_datetime(days)).sort_index()
    holidays = holidays or set()
    end = s.index.max()
    out = Completeness(end.date())
    for back in range(max_days):
        day = end - pd.Timedelta(days=back)
        prior = [day - pd.Timedelta(weeks=w) for w in range(1, 5)]
        ref = s.reindex(prior).dropna()
        if len(ref) < 2 or day not in s.index:
            break
        ratio = s[day] / max(float(ref.median()), 1e-12)
        if ratio >= threshold:
            break
        if day.date() in holidays:
            out.detail.append(f"{day.date()} is {ratio:.0%} of a normal {day:%A}, but it is a holiday: kept")
            break
        out.excluded_days.append(day.date())
        out.complete_end = (day - pd.Timedelta(days=1)).date()
        out.detail.append(f"{day.date()} has {ratio:.0%} of a normal {day:%A}'s volume: looks incomplete, excluded")
    return out


def _volume_cols(t: pd.DataFrame) -> tuple[str, str]:
    return ("d0", "d1") if "d0" in t else ("n0", "n1")


def _label_key(value) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value).lower())


def detect_rename(table: pd.DataFrame, dim: str, min_share: float = 0.01, tolerance: float = 0.35) -> Optional[str]:
    """A value that vanished and another that appeared with a similar share, or two spellings of
    one label with one of them new or gone: likely a rename."""
    c0, c1 = _volume_cols(table)
    s0 = table[c0] / max(table[c0].sum(), 1e-12)
    s1 = table[c1] / max(table[c1].sum(), 1e-12)
    vanished = table[(s0 >= min_share) & (table[c1] == 0)]
    new = table[(s1 >= min_share) & (table[c0] == 0)]
    for _, old in vanished.iterrows():
        for _, nw in new.iterrows():
            a, b = float(s0[old.name]), float(s1[nw.name])
            if abs(b - a) / a <= tolerance:
                return (f"values changed between windows: '{old[dim]}' ({a:.0%} of baseline) vanished and "
                        f"'{nw[dim]}' ({b:.0%} of current) appeared -- likely a rename; fix the model or map them")
    # A rename partway through a window leaves both spellings in it, so neither vanishes.
    spellings: dict[str, list] = {}
    for _, row in table.iterrows():
        spellings.setdefault(_label_key(row[dim]), []).append(row)
    for rows in spellings.values():
        if len(rows) > 1 and any(r[c0] == 0 or r[c1] == 0 for r in rows):
            names = " and ".join(f"'{r[dim]}'" for r in rows)
            return (f"{names} are one label spelled two ways, and one spelling is new or gone -- likely a "
                    "rename; fix the model or map them")
    return None


def detect_leakage(table: pd.DataFrame, dim: str, kind: str, coverage: float = 0.8) -> Optional[str]:
    """Is this dimension defined by the outcome it is supposed to explain?"""
    proportion = kind == "ratio" and bool(((table["n0"] <= table["d0"]) & (table["n1"] <= table["d1"])).all())
    if proportion:
        live = table[table["d0"] > 0]
        rate = live["n0"] / live["d0"]
        deterministic = live[(rate <= 0.001) | (rate >= 0.999)]
        share = deterministic["d0"].sum() / max(live["d0"].sum(), 1e-12)
        has_both = ((rate <= 0.001).any() and (rate >= 0.999).any())
        if share >= coverage and has_both:
            vals = ", ".join(f"'{v}' {r:.0%}" for v, r in zip(live[dim], rate))
            return f"looks derived from the outcome: rates are all ~0% or ~100% ({vals})"
        return None
    # Level metrics and non-proportion ratios (revenue per session): values that never carry any of
    # the metric in a low-cardinality dimension.
    if len(table) <= 5:
        zero = table[(table["n0"] == 0) & (table["n1"] == 0)]
        if len(zero) and table["n0"].sum() > 0:
            vals = ", ".join(f"'{v}'" for v in zero[dim])
            return f"looks derived from the outcome: {vals} never carry any of the metric"
    return None


@dataclass
class DimensionScreen:
    excluded: dict[str, str]
    notes: list[str]


def screen_dimensions(tables: dict[frozenset, pd.DataFrame], dims: list[str], kind: str,
                      meta: dict[str, dict]) -> DimensionScreen:
    excluded, notes = {}, []
    for d in dims:
        tag = (meta.get(d) or {}).get("rca")
        if tag == "outcome":
            excluded[d] = "declared meta rca: outcome"
            continue
        t = tables.get(frozenset({d}))
        if t is None:
            continue
        reason = detect_rename(t, d) or detect_leakage(t, d, kind)
        if reason and tag == "include":
            notes.append(f"`{pretty_dimension(d)}` {reason}, but it is tagged rca: include, so it was kept")
        elif reason:
            excluded[d] = "preflight: " + reason
    return DimensionScreen(excluded, notes)


def holiday_dates(calendar) -> set[dt.date]:
    h = getattr(calendar, "holidays", None)
    return set(h["date"]) if h is not None and len(h) else set()


def volume_series(daily: pd.DataFrame, components: list[str]) -> pd.Series:
    """The series to check for completeness: the denominator if there is one (traffic), else the metric."""
    return daily[components[-1]].astype(float)

